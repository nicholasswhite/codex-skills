from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import sys


TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))
import gist_capsule as capsule  # noqa: E402


class GistCapsuleTests(unittest.TestCase):
    def make_skill(self, root: Path) -> Path:
        skill = root / "example-skill"
        (skill / "scripts").mkdir(parents=True)
        (skill / "assets").mkdir()
        (skill / "__pycache__").mkdir()
        (skill / "outputs").mkdir()
        (skill / "SKILL.md").write_text("---\nname: example-skill\ndescription: Test.\n---\n", encoding="utf-8")
        (skill / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")
        (skill / "assets" / "data.bin").write_bytes(b"\x00\xff\x01")
        (skill / "assets" / "whitespace.txt").write_bytes(b"\n")
        (skill / "__pycache__" / "ignored.pyc").write_bytes(b"ignored")
        (skill / "outputs" / "ignored.txt").write_text("ignored", encoding="utf-8")
        (skill / "outputs" / ".gitkeep").write_bytes(b"")
        return skill

    def build(self, root: Path) -> tuple[Path, Path]:
        skill = self.make_skill(root)
        output = root / "capsule"
        capsule.build_capsule(
            skill,
            output,
            source_repo="https://github.com/example/codex-skills",
            source_ref="0123456789abcdef0123456789abcdef01234567",
        )
        return skill, output

    def test_round_trip_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill, output = self.build(root)
            destination = root / "restored"

            manifest = capsule.verify_capsule(output, skill)
            capsule.unpack_capsule(output, destination)

            self.assertEqual("codex-skill-gist/v1", manifest["schema"])
            self.assertEqual((skill / "SKILL.md").read_bytes(), (destination / "SKILL.md").read_bytes())
            self.assertEqual(b"\x00\xff\x01", (destination / "assets" / "data.bin").read_bytes())
            self.assertEqual(b"\n", (destination / "assets" / "whitespace.txt").read_bytes())
            self.assertFalse((destination / "__pycache__").exists())
            self.assertFalse((destination / "outputs" / "ignored.txt").exists())
            self.assertTrue((destination / "outputs" / ".gitkeep").is_file())

            entries = {item["path"]: item for item in manifest["files"]}
            self.assertEqual("empty", entries["outputs/.gitkeep"]["encoding"])
            self.assertEqual("base64", entries["assets/whitespace.txt"]["encoding"])
            for payload in output.glob("blob-*"):
                self.assertTrue(payload.read_bytes().strip(), payload.name)

    def test_rejects_traversal_and_case_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, output = self.build(root)
            manifest_path = output / "CAPSULE.v1.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"][0]["path"] = "../escape"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(capsule.CapsuleError, "unsafe manifest path"):
                capsule.reconstruct(output)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, output = self.build(root)
            manifest_path = output / "CAPSULE.v1.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            duplicate = dict(manifest["files"][0])
            duplicate["path"] = manifest["files"][0]["path"].upper()
            manifest["files"].append(duplicate)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(capsule.CapsuleError, "case-colliding"):
                capsule.reconstruct(output)

    def test_rejects_windows_drive_ads_device_and_normalized_paths(self) -> None:
        unsafe = (
            "D:outside.txt",
            "file:stream",
            "CON",
            "folder/aux.txt",
            "COM¹.txt",
            "LPT³.log",
            "CON .txt",
            "COM1 .html",
            "trailing.",
            "trailing ",
        )
        for relative in unsafe:
            with self.subTest(relative=relative):
                with self.assertRaises(capsule.CapsuleError):
                    capsule._safe_relative_path(relative)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, output = self.build(root)
            manifest_path = output / "CAPSULE.v1.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            duplicate = dict(manifest["files"][0])
            manifest["files"][0]["path"] = "caf\u00e9.txt"
            duplicate["path"] = "cafe\u0301.txt"
            manifest["files"].append(duplicate)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(capsule.CapsuleError, "case-colliding"):
                capsule.reconstruct(output)

    def test_rejects_tampering_and_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, output = self.build(root)
            payload = next(output.glob("blob-*.txt"))
            payload.write_bytes(payload.read_bytes() + b"tampered")
            with self.assertRaisesRegex(capsule.CapsuleError, "mismatch|exceeds"):
                capsule.reconstruct(output)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, output = self.build(root)
            destination = root / "exists"
            destination.mkdir()
            with self.assertRaisesRegex(capsule.CapsuleError, "already exists"):
                capsule.unpack_capsule(output, destination)

    def test_rejects_repeated_sources_and_oversized_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, output = self.build(root)
            manifest_path = output / "CAPSULE.v1.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"][0]["sources"] *= capsule.MAX_SOURCES_PER_FILE + 1
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(capsule.CapsuleError, "payload sources"):
                capsule.reconstruct(output)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, output = self.build(root)
            manifest_path = output / "CAPSULE.v1.json"
            with manifest_path.open("ab") as stream:
                stream.write(b" " * capsule.MAX_MANIFEST_BYTES)
            with self.assertRaisesRegex(capsule.CapsuleError, "manifest-size"):
                capsule.reconstruct(output)

    def test_source_must_not_contain_links(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill = self.make_skill(root)
            linked = skill / "scripts" / "linked.py"
            try:
                linked.symlink_to(skill / "scripts" / "run.py")
            except OSError:
                self.skipTest("File symlinks are unavailable on this host")
            with self.assertRaisesRegex(capsule.CapsuleError, "unsupported file"):
                capsule.source_files(skill)


if __name__ == "__main__":
    unittest.main()

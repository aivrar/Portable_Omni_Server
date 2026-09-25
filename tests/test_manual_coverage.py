"""Operator usage manual coverage: pages exist, are fully written, and name shipped topics."""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
MANUAL = ROOT / "manual"

# Whole-word stubs. "Soon" is a real product badge, not a writing stub.
_STUB_RE = re.compile(r"\b(TODO|TBD|placeholder)\b", re.IGNORECASE)
_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")

# Criterion 3 destinations plus launch/special-usage phrases from the goal.
_REQUIRED_PHRASES = (
    "Linux distro",
    "Windows application",
    "Windows 10",
    "WSL2",
    "WebView2",
    "NVIDIA GPU",
    "disk headroom",
    "keep the app folder together",
    "Omni_Studio.exe",
    "linbox-Omni_Studio",
    "Home",
    "Chat",
    "Media library",
    "Workflows",
    "Engine & queue",
    "Voice & TTS",
    "Soon",
    "Audio Lab",
    "Music 3",
    "MOSS",
    "Model library",
    "Runtime",
    "Testing",
    "Logs",
    "Shutdown",
    "HuggingFace",
    "GPU placement",
    "single",
    "auto",
    "manual",
    "analyze-before-run",
    "valid: false",
    "omni-cli.bat",
    "omni-cli",
    "CLI",
)


def _manual_pages() -> list[Path]:
    return sorted(p for p in MANUAL.glob("*.md") if p.is_file())


def _word_count(text: str) -> int:
    return len(text.split())


class ManualCoverageTests(unittest.TestCase):
    def test_manual_directory_has_multiple_full_pages(self):
        pages = _manual_pages()
        self.assertTrue(MANUAL.is_dir(), "manual/ directory is missing")
        self.assertGreaterEqual(
            len(pages),
            8,
            f"expected a multi-page manual, found {len(pages)} markdown files",
        )

        short = []
        heading_only = []
        for page in pages:
            text = page.read_text(encoding="utf-8")
            words = _word_count(text)
            if words < 250:
                short.append(f"{page.name}:{words}")
            paragraphs = [
                ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")
            ]
            if len(paragraphs) < 8:
                heading_only.append(page.name)
        self.assertFalse(short, f"pages too short to be operator how-to prose: {short}")
        self.assertFalse(heading_only, f"pages look like heading-only stubs: {heading_only}")

    def test_manual_rejects_todo_tbd_placeholder_stubs(self):
        hits = []
        for page in _manual_pages():
            text = page.read_text(encoding="utf-8")
            for match in _STUB_RE.finditer(text):
                line_no = text[: match.start()].count("\n") + 1
                hits.append(f"{page.name}:{line_no}:{match.group(0)}")
        self.assertFalse(hits, f"manual contains stub language: {hits}")

    def test_manual_covers_required_operator_topics(self):
        combined = "\n".join(p.read_text(encoding="utf-8") for p in _manual_pages())
        lowered = combined.lower()
        missing = [phrase for phrase in _REQUIRED_PHRASES if phrase.lower() not in lowered]
        self.assertFalse(missing, f"manual is missing required topic phrases: {missing}")

    def test_manual_index_links_operator_pages(self):
        index = MANUAL / "README.md"
        self.assertTrue(index.is_file(), "manual/README.md index is required")
        text = index.read_text(encoding="utf-8")
        for name in (
            "01-what-omni-studio-is.md",
            "03-home.md",
            "04-chat.md",
            "05-media-library.md",
            "08-workflows.md",
            "09-engine-and-queue.md",
            "10-voice-and-tts.md",
            "17-shutdown.md",
            "18-cli.md",
        ):
            self.assertIn(name, text, f"index does not link {name}")

    def test_local_manual_and_api_links_resolve(self):
        pages = [ROOT / "README.md", *_manual_pages(), *(ROOT / "docs").glob("*.md")]
        broken = []
        for page in pages:
            text = page.read_text(encoding="utf-8")
            for target in _LINK_RE.findall(text):
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                relative = unquote(target.split("#", 1)[0])
                if relative and not (page.parent / relative).is_file():
                    broken.append(f"{page.relative_to(ROOT)} -> {target}")
        self.assertFalse(broken, f"broken local documentation links: {broken}")


if __name__ == "__main__":
    unittest.main()

"""Export the maintained manual into an existing GitHub wiki checkout.

Run: python tools/export_wiki.py PATH_TO_WIKI_CHECKOUT
This writes known manual pages/assets and navigation, without committing or
deleting unrelated wiki pages. Review the checkout diff before publishing.
"""

import argparse
from pathlib import Path
import re
import shutil
from urllib.parse import quote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
REPO = "https://github.com/aivrar/Portable_Omni_Server"
WIKI = REPO + "/wiki"
RAW = "https://raw.githubusercontent.com/wiki/aivrar/Portable_Omni_Server"
PAGES = {
    "README.md": "Home",
    "01-what-omni-studio-is.md": "What-is-Omni-Studio",
    "02-launch-and-first-run.md": "Installation-and-First-Run",
    "03-home.md": "Home-Workspace",
    "04-chat.md": "Chat",
    "05-media-library.md": "Media-Library",
    "06-model-library.md": "Model-Library",
    "07-runtime.md": "Runtime",
    "08-workflows.md": "Workflows",
    "09-engine-and-queue.md": "ComfyUI-Engine-and-Queue",
    "10-voice-and-tts.md": "Voice-and-TTS",
    "11-audio-lab.md": "Audio-Lab",
    "12-music.md": "Music-ACE-Step",
    "13-music-3.md": "Music-3-MiniMax",
    "14-moss.md": "MOSS",
    "15-testing.md": "Testing",
    "16-logs.md": "Logs",
    "17-shutdown.md": "Shutdown",
    "18-cli.md": "Command-Line-Interface",
    "19-gpu-placement.md": "GPU-Placement",
    "20-jobs-huggingface-and-special-usages.md": "Jobs-and-Audio-Composition",
    "21-feature-compatibility.md": "Feature-Compatibility",
    "screenshots.md": "Screenshots",
}


def rewrite_link(match):
    target = match.group(1)
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or target.startswith("#"):
        return match.group(0)
    local = (ROOT / "manual" / parsed.path).resolve()
    relative = local.relative_to(ROOT).as_posix()
    if not local.exists():
        raise ValueError(f"Missing manual link: {target}")
    if parsed.path in PAGES:
        url = WIKI + "/" + PAGES[parsed.path]
    elif relative.startswith("manual/images/"):
        url = RAW + "/" + quote(relative.removeprefix("manual/"))
    else:
        url = REPO + "/blob/main/" + quote(relative)
    if parsed.fragment:
        url += "#" + parsed.fragment
    return "](" + url + ")"


def export(destination):
    destination = destination.resolve()
    if not (destination / ".git").exists():
        raise ValueError("Destination must be a wiki Git checkout")
    if destination == ROOT or ROOT in destination.parents:
        raise ValueError("Keep the wiki checkout outside the source tree")
    manual = ROOT / "manual"
    if set(p.name for p in manual.glob("*.md")) != set(PAGES):
        raise ValueError("Update PAGES for the current manual before exporting")
    for filename, title in PAGES.items():
        content = (manual / filename).read_text(encoding="utf-8")
        content = re.sub(r"\]\(([^\s)]+)\)", rewrite_link, content)
        if title == "Home":
            content = content.replace("# Omni Studio usage manual", "# Portable Omni Server manual", 1)
            content = content.replace("\n\n", "\n\n![Omni Studio workspace](" + RAW + "/images/hero.png)\n\n", 1)
        (destination / (title + ".md")).write_text(content, encoding="utf-8", newline="\n")
    images = destination / "images"
    images.mkdir(exist_ok=True)
    for asset in (manual / "images").iterdir():
        if asset.is_file() and asset.suffix in {".png", ".json"}:
            shutil.copy2(asset, images / asset.name)
    sidebar = "## Manual\n\n" + "\n".join(
        f"- [{title.replace('-', ' ')}]({WIKI}/{title})" for title in PAGES.values()
    ) + "\n"
    (destination / "_Sidebar.md").write_text(sidebar, encoding="utf-8", newline="\n")
    footer = (
        f"[Download]({REPO}/releases/latest) · [Source]({REPO}) · "
        f"[Manual home]({WIKI})\n\n"
        "Maintained by [aivrar](https://github.com/aivrar). Built from "
        "[portable-linux-in-a-box](https://github.com/aivrar/portable-linux-in-a-box).\n"
    )
    (destination / "_Footer.md").write_text(footer, encoding="utf-8", newline="\n")
    print(f"Exported {len(PAGES)} manual pages and navigation to {destination}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    export(parser.parse_args().destination)

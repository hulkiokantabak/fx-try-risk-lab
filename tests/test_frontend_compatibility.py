"""Static compatibility and accessibility contracts for the browser UI."""

from __future__ import annotations

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
HTML_FILES = (DOCS / "index.html", DOCS / "methodology.html")


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.doctype = ""
        self.elements: list[tuple[str, dict[str, str]]] = []

    def handle_decl(self, declaration: str) -> None:
        self.doctype = declaration

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, {key: value or "" for key, value in attrs}))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def _parse(path: Path) -> _DocumentParser:
    parser = _DocumentParser()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser


class FrontendCompatibilityTests(unittest.TestCase):
    def test_documents_have_a_sound_accessible_shell(self) -> None:
        for path in HTML_FILES:
            with self.subTest(path=path.name):
                parsed = _parse(path)
                ids = [attrs["id"] for _, attrs in parsed.elements if attrs.get("id")]
                self.assertEqual(parsed.doctype.lower(), "doctype html")
                self.assertEqual(len(ids), len(set(ids)), "duplicate HTML IDs")

                html = next(attrs for tag, attrs in parsed.elements if tag == "html")
                self.assertTrue(html.get("lang"), "the document language must be declared")
                self.assertTrue(any(tag == "main" for tag, _ in parsed.elements))
                self.assertTrue(any(tag == "h1" for tag, _ in parsed.elements))
                self.assertTrue(
                    any(attrs.get("class") == "skip-link" for tag, attrs in parsed.elements if tag == "a"),
                    "keyboard users need a skip link",
                )

                id_set = set(ids)
                for tag, attrs in parsed.elements:
                    labelled_by = attrs.get("aria-labelledby", "")
                    for referenced_id in labelled_by.split():
                        self.assertIn(referenced_id, id_set, f"{tag} references missing #{referenced_id}")
                    if tag == "label" and attrs.get("for"):
                        self.assertIn(attrs["for"], id_set, f"label references missing #{attrs['for']}")
                    if tag == "button":
                        self.assertIn(attrs.get("type"), {"button", "submit", "reset"})
                    if tag == "a" and attrs.get("target") == "_blank":
                        rel = set(attrs.get("rel", "").split())
                        self.assertTrue({"noopener", "noreferrer"}.issubset(rel))

    def test_local_links_assets_and_fragments_resolve(self) -> None:
        for path in HTML_FILES:
            parsed = _parse(path)
            own_ids = {attrs["id"] for _, attrs in parsed.elements if attrs.get("id")}
            for tag, attrs in parsed.elements:
                attribute = "href" if tag in {"a", "link"} else "src" if tag == "script" else None
                if not attribute or not attrs.get(attribute):
                    continue
                value = attrs[attribute]
                split = urlsplit(value)
                if split.scheme or split.netloc or value.startswith("data:"):
                    continue
                with self.subTest(document=path.name, reference=value):
                    if not split.path:
                        self.assertIn(split.fragment, own_ids)
                        continue
                    target = (path.parent / split.path).resolve()
                    self.assertTrue(target.is_relative_to(DOCS.resolve()), "local asset escapes docs/")
                    self.assertTrue(target.is_file(), f"missing local target {target}")
                    if split.fragment and target.suffix == ".html":
                        target_ids = {
                            item["id"]
                            for _, item in _parse(target).elements
                            if item.get("id")
                        }
                        self.assertIn(split.fragment, target_ids)

    def test_javascript_required_dom_targets_exist(self) -> None:
        javascript = (DOCS / "app.js").read_text(encoding="utf-8")
        required_ids = set(re.findall(r'byId\("([^"]+)"\)', javascript))
        index_ids = {
            attrs["id"]
            for _, attrs in _parse(DOCS / "index.html").elements
            if attrs.get("id")
        }
        self.assertFalse(required_ids - index_ids, f"missing required DOM targets: {sorted(required_ids - index_ids)}")
        self.assertNotIn(".at(", javascript, "avoid an unnecessary Safari 15.4+ dependency")
        self.assertNotIn(".replaceAll(", javascript, "avoid an unnecessary recent String API dependency")

    def test_security_and_motion_fallbacks_are_declared(self) -> None:
        for path in HTML_FILES:
            parsed = _parse(path)
            policies = [
                attrs.get("content", "")
                for tag, attrs in parsed.elements
                if tag == "meta" and attrs.get("http-equiv", "").lower() == "content-security-policy"
            ]
            with self.subTest(path=path.name):
                self.assertEqual(len(policies), 1)
                self.assertIn("object-src 'none'", policies[0])
                self.assertIn("base-uri 'self'", policies[0])
                self.assertNotIn("'unsafe-inline'", policies[0])
                self.assertNotIn("'unsafe-eval'", policies[0])

        css = (DOCS / "style.css").read_text(encoding="utf-8")
        self.assertIn(":focus-visible", css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)


if __name__ == "__main__":
    unittest.main()

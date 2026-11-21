"""Local checks derived from the live guide; not the host's own validator."""

import json
import math
import re
from pathlib import Path

TYPES = {"field", "group", "card", "grid", "tabs", "heading", "text", "code", "badge",
         "alert", "markdown", "divider", "image", "link", "table", "progress", "button"}
TONES = {"default", "primary", "success", "warning", "danger", "muted"}


def validate_ui(ui, inputs, *, result=False):
    if isinstance(ui, list):
        nodes, columns = ui, 1
    else:
        assert isinstance(ui, dict)
        assert ui.get("layout", "stack") in {"stack", "grid"}
        assert ui.get("width", "normal") in {"normal", "wide", "full"}
        columns = ui.get("columns", 1) if ui.get("layout") == "grid" else 1
        assert type(columns) is int and 1 <= columns <= 4
        nodes = ui["components"]
    fields = []
    count = 0

    def visit(nodes, depth, columns):
        nonlocal count
        assert isinstance(nodes, list)
        assert depth <= 8
        for node in nodes:
            count += 1
            assert count <= 200
            kind = node["type"]
            assert kind in TYPES
            style = node.get("style", {})
            assert set(style) <= {"tone", "align", "size", "variant", "span"}
            assert style.get("tone", "default") in TONES
            assert style.get("align", "left") in {"left", "center", "right"}
            assert style.get("size", "normal") in {"small", "normal", "large"}
            assert style.get("variant", "solid") in {"solid", "outline", "soft"}
            assert type(style.get("span", 1)) is int and 1 <= style.get("span", 1) <= columns
            if kind == "field":
                assert not result, "result UI cannot contain field"
                assert node["name"] in inputs
                assert node["name"] not in fields
                fields.append(node["name"])
            if kind == "table":
                assert 1 <= len(node["headers"]) <= 30
                assert len(node["rows"]) <= 200
                assert all(isinstance(h, str) for h in node["headers"])
                assert all(isinstance(row, list) and len(row) == len(node["headers"])
                           and all(isinstance(cell, str) for cell in row) for row in node["rows"])
            if kind == "button":
                assert node["action"] in {"run", "save", "reset"}
                assert set(node.get("params", {})) <= inputs.keys()
                for name, value in node.get("params", {}).items():
                    spec = inputs[name]
                    if spec["type"] == "number":
                        assert type(value) in {int, float} and math.isfinite(value)
                        assert spec.get("min", -math.inf) <= value <= spec.get("max", math.inf)
                    else:
                        assert isinstance(value, str)
            if kind == "image":
                assert node["src"].startswith(("https://", "http://", "data://", "plugin://", "data:image/png;base64,"))
                assert node.get("alt")
            if "children" in node:
                child_columns = node.get("columns", 1) if kind == "grid" else 1
                assert type(child_columns) is int and 1 <= child_columns <= 4
                visit(node["children"], depth + 1, child_columns)

    visit(nodes, 1, columns)
    return {"components": count, "fields": fields}


def validate_manifest(root):
    path = Path(root) / "plugin.json"
    assert path.stat().st_size <= 65536
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert type(manifest["schema_version"]) is int and manifest["schema_version"] == 1
    assert re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", manifest["id"])
    assert 1 <= len(manifest["name"]) <= 100
    assert 1 <= len(manifest["version"]) <= 50
    assert len(manifest.get("description", "")) <= 4000
    assert len(manifest.get("author", "")) <= 100
    entry = manifest["entry"]
    assert entry.endswith(".py") and not Path(entry).is_absolute() and ".." not in Path(entry).parts
    assert (Path(root) / entry).is_file()
    assert type(manifest["timeout"]) is int and manifest["timeout"] >= 0
    assert len(manifest["inputs"]) <= 50
    inputs = {p["name"]: p for p in manifest["inputs"]}
    assert len(inputs) == len(manifest["inputs"])
    assert all(re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", name) for name in inputs)
    ui = validate_ui(manifest["ui"], inputs)
    assert set(ui["fields"]) == inputs.keys()
    return manifest, inputs, ui


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    manifest, inputs, ui = validate_manifest(root)
    print(json.dumps({"manifest": "PASS", "inputs": len(inputs), **ui}, ensure_ascii=False))

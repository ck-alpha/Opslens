import json
import shutil
from pathlib import Path

import duckdb
import pytest

from opslens.knowledge import KnowledgeCorpus
from opslens.knowledge.citations import chunk_fingerprint, content_hash
from opslens.knowledge.manifest import load_manifest
from opslens.knowledge.metric_renderer import MetricKnowledgeRenderer
from opslens.metrics import MetricRegistry

ROOT = Path(__file__).parents[2]


@pytest.fixture
def knowledge(tmp_path):
    return Path(shutil.copytree(ROOT / "data/knowledge", tmp_path / "knowledge"))


def test_real_rendering(registry, corpus):
    renderer = MetricKnowledgeRenderer()
    assert len(renderer.render(registry)) == 8
    docs = renderer.render(registry, include_deprecated=True)
    assert len(docs) == 9
    assert len(corpus.documents) == 14
    assert docs == renderer.render(
        MetricRegistry(list(reversed(registry.all()))), include_deprecated=True
    )
    for doc in docs:
        definition = registry.get(doc.metric_ids[0], doc.version)
        assert doc.doc_id == f"metric:{definition.metric_id}"
        assert doc.status == definition.status
        for text in (
            definition.description,
            definition.time_semantics.time_column,
            definition.refund_policy,
            *definition.dependencies,
            *definition.allowed_dimensions,
        ):
            assert text in doc.text
        assert "filters:" in doc.text
        assert doc.content_sha256 == content_hash(doc.text)
    copy = corpus.documents
    copy[0].tags.append("mutated")
    assert "mutated" not in corpus.documents[0].tags


@pytest.mark.parametrize(
    "change",
    [
        {"relative_path": "/tmp/doc.md"},
        {"relative_path": "../outside.md"},
        {"relative_path": "documents/../doc.md"},
        {"relative_path": "C:\\file.md"},
        {"relative_path": "documents/a.py"},
        {"relative_path": "documents/missing.md"},
        {"extra": True},
        {"version": 1},
        {"status": "active"},
        {"doc_id": "metric:bad"},
    ],
)
def test_manifest_rejects(knowledge, change):
    path = knowledge / "manifest.json"
    data = json.loads(path.read_text())
    data["documents"][0].update(change)
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_manifest(knowledge)


@pytest.mark.parametrize(
    "mode", ["duplicate", "active", "empty", "symlink", "non_synthetic", "unknown_metric"]
)
def test_document_errors(knowledge, registry, mode, tmp_path):
    path = knowledge / "manifest.json"
    data = json.loads(path.read_text())
    entry = data["documents"][0]
    if mode in ("duplicate", "active"):
        data["documents"].append(entry | ({"version": "2.0"} if mode == "active" else {}))
        path.write_text(json.dumps(data))
    elif mode == "unknown_metric":
        entry["metric_ids"] = ["missing_metric"]
        path.write_text(json.dumps(data))
    else:
        file = knowledge / entry["relative_path"]
        if mode == "symlink":
            target = tmp_path / "outside.md"
            target.write_text(file.read_text())
            file.unlink()
            file.symlink_to(target)
        else:
            file.write_text("   " if mode == "empty" else "Undeclared source")
    with pytest.raises(ValueError):
        KnowledgeCorpus(registry, knowledge)


def test_fingerprint(knowledge, registry):
    before = KnowledgeCorpus(registry, knowledge).fingerprint
    manifest = knowledge / "manifest.json"
    data = json.loads(manifest.read_text())
    data["documents"].reverse()
    for entry in data["documents"]:
        entry["tags"].reverse()
        entry["metric_ids"].reverse()
    manifest.write_text(json.dumps(data, indent=4))
    assert KnowledgeCorpus(registry, knowledge).fingerprint == before
    file = knowledge / data["documents"][0]["relative_path"]
    file.write_bytes(file.read_bytes() + b"\n")
    assert KnowledgeCorpus(registry, knowledge).fingerprint != before
    after_whitespace = KnowledgeCorpus(registry, knowledge).fingerprint
    definitions = registry.all()
    definitions[0] = definitions[0].model_copy(update={"description": "changed business meaning"})
    assert KnowledgeCorpus(MetricRegistry(definitions), knowledge).fingerprint != after_whitespace
    d = registry.all()
    for definition in d:
        definition.allowed_dimensions.reverse()
        definition.aliases.reverse()
    assert (
        KnowledgeCorpus(MetricRegistry(d), knowledge).fingerprint
        == KnowledgeCorpus(registry, knowledge).fingerprint
    )


def test_dictionary_matches_real_schema(demo_db):
    text = (ROOT / "data/knowledge/documents/data_dictionary.md").read_text()
    with duckdb.connect(str(demo_db), read_only=True) as conn:
        columns = conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_name != '_demo_metadata'"
        ).fetchall()
    assert len({table for table, _ in columns}) == 5
    assert all(f"{table}.{column}" in text for table, column in columns)


def test_chunk_identity():
    a = chunk_fingerprint("refund_policy", "1.0", "same  text\n")
    assert a == chunk_fingerprint("refund_policy", "1.0", "same text")
    for doc, version, text in [
        ("other", "1.0", "same text"),
        ("refund_policy", "2.0", "same text"),
        ("refund_policy", "1.0", "changed text"),
    ]:
        assert a != chunk_fingerprint(doc, version, text)
    assert len(a) == 64

import asyncio
import json
from html import unescape

import panel as pn

from opslens.ui.app import Console, create_app, markdown, safe_json


def test_factory_and_registry_options(product_service):
    app = create_app(product_service)
    assert isinstance(app, pn.template.MaterialTemplate)
    ui = Console(product_service)
    ui.metric.value = "gross_gmv"
    assert "orders.channel_id" not in ui.dimensions.options
    assert (
        ui.sql.max_length == product_service.runtime.pipeline.validation.sql_policy.max_sql_length
    )
    assert not ui.can_resume if hasattr(ui, "can_resume") else ui.resume_button.disabled


def test_all_five_ui_journeys(product_service):
    ui = Console(product_service)

    async def exercise():
        await ui.perform(ui.clarify)
        assert "CLARIFICATION_REQUIRED" in ui.message.object
        ui.metric_option.value = "gross_gmv@1.0"
        await ui.perform(ui.select_metric)
        assert "selected only" in ui.message.object
        assert ui.state.run_id is None
        ui.recipe.value = "gross_gmv_august"
        await ui.perform(ui.run_demo)
        assert "VERIFIED" in ui.message.object
        for label in ("Open Evidence", "Open Observability / Event Timeline", "Helpful"):
            button = next(b for b in reversed(ui.buttons) if b.name == label)
            # Exercise the same registered callback path used by a browser click.
            callback = button.param.watchers["clicks"]["value"][-1].fn
            await callback(None)
        ui.sql.value = "DELETE FROM orders"
        await ui.perform(ui.verify)
        assert "BLOCKED BY POLICY" in ui.message.object
        ui.recipe.value = "known_limitation_demo"
        await ui.perform(ui.run_demo)
        assert "NEEDS_REVIEW" in ui.message.object
        await ui.perform(ui.refresh_history)
        assert ui.history_run.options
        await ui.perform(ui.open_history)
        assert ui.state.run_id

    asyncio.run(exercise())


def test_double_submit_and_failure_boundary(product_service, monkeypatch):
    ui = Console(product_service)
    entered = 0

    async def action():
        nonlocal entered
        entered += 1
        await asyncio.sleep(0.02)
        assert ui.run_demo_button.disabled
        await ui.run_demo()

    async def double():
        await asyncio.gather(ui.perform(action), ui.perform(action))

    asyncio.run(double())
    assert entered == 1 and ui.state.counter == 1
    assert not ui.run_demo_button.disabled

    def unavailable(*args, **kwargs):
        raise RuntimeError("password postgresql://private Traceback /home/private")

    monkeypatch.setattr(product_service, "start", unavailable)
    asyncio.run(ui.perform(ui.run_demo))
    assert ui.message.object.startswith("PRODUCT_ACTION_FAILED")
    for secret in ("password", "postgresql://", "Traceback", "/home/"):
        assert secret not in ui.message.object
    assert not ui.state.busy


def test_telemetry_degraded_ui(product_service, monkeypatch):
    monkeypatch.setattr(product_service, "record_event", lambda *a, **k: False)
    ui = Console(product_service)
    ui.recipe.value = "gross_gmv_august"
    asyncio.run(ui.perform(ui.run_demo))
    assert "VERIFIED" in ui.message.object
    assert ui.state.telemetry_degraded and "degraded" in ui.telemetry.object


def test_xss_and_safe_json():
    attack = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
    pane = markdown(attack + " [click](javascript:alert(3))")
    html = unescape(pane._transform_object(pane.object)["object"])
    assert "<script>" not in html and "<img" not in html
    assert 'href="javascript:' not in html
    value = safe_json({"value": attack})
    assert isinstance(value, pn.pane.Str)
    assert json.loads(value.object)["value"] == attack


def test_local_generation_ui_with_fake_provider(product_service, monkeypatch):
    import hashlib

    from opslens.llm.models import Candidate, Generation, GenerationMetadata
    from opslens.llm.provider import OllamaProvider

    sql = product_service.load_demo_recipe("gross_gmv_august").runtime_request.candidate_sql

    def generate(self, prompt, attempt):
        return Generation(
            candidate=Candidate(sql=sql),
            metadata=GenerationMetadata(
                provider="fake",
                model="fake",
                prompt_hash=hashlib.sha256(prompt.encode()).hexdigest(),
                attempt=attempt,
                duration_ms=1.0,
                status="SUCCESS",
            ),
        )

    monkeypatch.setenv("OPSLENS_OLLAMA_MODEL", "fake")
    monkeypatch.setattr(OllamaProvider, "generate", generate)
    ui = Console(product_service)
    ui.metric.value = "gross_gmv"
    asyncio.run(ui.perform(ui.generate_local))
    assert ui.message.object.startswith("VERIFIED")
    assert "attempts=1" in ui.local_status.object
    assert "repairs=0" in ui.local_status.object
    assert product_service.get_run(ui.state.run_id).generation[0].provider == "fake"

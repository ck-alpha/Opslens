"""Session-scoped Panel console; all analysis executes through ProductService."""

import asyncio
import io
import json
from datetime import datetime
from typing import cast

import panel as pn

from opslens.evidence.models import MetricBinding
from opslens.product.config import ProductConfig, readiness
from opslens.product.events import EventType
from opslens.product.presenters import product_status
from opslens.product.service import ProductService
from opslens.reporting.markdown import render_markdown
from opslens.runtime.request import RuntimeAnalysisRequest
from opslens.ui.state import SessionState
from opslens.validation.intent import AnalysisIntent, TimeWindow

BANNER = (
    "Synthetic Demo Data · Local LLM: opt-in · Generated ≠ Verified · "
    "Validation scope: limited"
)
HELP = """# Verification-first Operations Analytics
先明确指标，再执行验证。选择 Demo Analyses 中的 August Gross GMV 开始。

**VERIFIED**：在当前验证范围内通过，且存在已验证事实。
**NEEDS REVIEW**：证据、语义或运行上下文存在未解决项，需要人工复核。
**BLOCKED BY POLICY**：SQL 被治理策略拒绝，未产生已验证事实。

Local LLM Analysis 支持本地候选生成；先确认指标、时间窗口与维度，再生成并验证。
不确定性与原始 reason codes 会保留在报告中。
"""
LIMITATIONS = (
    "Some channel_id dimensions and time-boundary semantics remain unsupported. "
    "Retrieved citations may be irrelevant or incomplete. Verification covers only "
    "the configured SQL policies, metric contracts, and available evidence; "
    "generated SQL and explanations still require review."
)


def markdown(value: str):
    return pn.pane.Markdown(
        value,
        renderer="markdown-it",
        renderer_options={"html": False, "linkify": False},
        sizing_mode="stretch_width",
    )


def safe_json(value):
    # Str escapes HTML; JSON is displayed as text, never interpreted as markup.
    return pn.pane.Str(
        json.dumps(value, ensure_ascii=False, indent=2, default=str), sizing_mode="stretch_width"
    )


class Console:
    def __init__(self, service: ProductService, config: ProductConfig | None = None):
        self.service, self.config = service, config or ProductConfig()
        self.state = SessionState()
        self.message = pn.pane.Str("READY_TO_RUN — Select a demo or verify candidate SQL.")
        self.telemetry = pn.pane.Str("")
        self.detail = pn.Column(markdown("Run Detail — no run selected"))
        self.clarification = pn.Column()
        self.history_view = pn.Column()
        self.system_view = pn.Column(markdown(LIMITATIONS))
        self.buttons: list = []
        self.question = pn.widgets.TextInput(
            label="Ask / Clarify a Metric", value="8 月 GMV 是多少？", max_length=2000
        )
        self.metric_option = pn.widgets.Select(label="Candidate Metric / Version", options=[])
        self.recipe = pn.widgets.Select(
            label="Demo Analysis Catalog",
            options={r.title: r.recipe_id for r in service.list_demo_recipes()},
        )
        self.recipe_description = pn.pane.Str("")
        self.recipe.param.watch(self.recipe_changed, "value")
        self.recipe_changed(None)
        definitions = service.registry.all()
        self.metric = pn.widgets.Select(
            label="Metric ID", options=sorted({d.metric_id for d in definitions})
        )
        self.version = pn.widgets.Select(label="Metric Version", options=[])
        self.dimensions = pn.widgets.MultiChoice(label="Dimensions", options=[])
        self.grain = pn.widgets.Select(label="Grain", options={"No time grouping": None})
        self.time_semantics = pn.pane.Str("")
        self.output = pn.widgets.TextInput(label="Metric Output Column", value="gross_gmv")
        self.start = pn.widgets.TextInput(
            label="Time Start [inclusive]", value="2026-08-01T00:00:00"
        )
        self.end = pn.widgets.TextInput(label="Time End [exclusive]", value="2026-09-01T00:00:00")
        self.mode = pn.widgets.Select(
            label="Validation Mode", options=["METRIC_ANALYSIS", "DATA_QUERY"]
        )
        self.sql = pn.widgets.TextAreaInput(
            label="Candidate SQL — Verify SQL (not Generate SQL)",
            height=190,
            max_length=service.runtime.pipeline.validation.sql_policy.max_sql_length,
            value=service.load_demo_recipe("gross_gmv_august").runtime_request.candidate_sql,
        )
        self.metric.param.watch(self.metric_changed, "value")
        self.version.param.watch(self.version_changed, "value")
        self.metric_changed(None)
        self.run_demo_button = self.button("Run Demo Analysis", self.run_demo)
        self.verify_button = self.button("Verify SQL", self.verify)
        self.resume_button = self.button("Resume Run", self.resume)
        self.rerun_button = self.button("Rerun Task", self.rerun)
        self.resume_button.disabled = self.rerun_button.disabled = True
        self.history_status = pn.widgets.Select(
            label="Runtime Status Filter",
            options=[
                "ALL",
                "PENDING",
                "RUNNING",
                "INTERRUPTED",
                "SUCCEEDED",
                "FAILED",
                "NEEDS_REVIEW",
                "WAITING_INPUT",
            ],
        )
        self.history_limit = pn.widgets.IntInput(
            label="Recent Run Limit (1–100)", value=20, start=1, end=100
        )
        self.history_run = pn.widgets.Select(label="Open Run Detail", options=[])
        self.local_question = pn.widgets.TextAreaInput(
            label="Local LLM Analysis — Question", value="2026年8月毛GMV是多少？", max_length=2000
        )
        self.local_status = pn.pane.Str("先在 Advanced SQL Verification 确认指标、时间与维度。")
        analysis = pn.Tabs(
            (
                "Ask / Clarify a Metric",
                pn.Column(
                    self.question,
                    pn.Row(
                        self.button("Clarify Metric", self.clarify),
                        self.button("Lookup Business Knowledge", self.lookup),
                    ),
                    self.clarification,
                    self.metric_option,
                    self.button("Confirm Metric Selection", self.select_metric),
                ),
            ),
            (
                "Demo Analyses",
                pn.Column(self.recipe, self.recipe_description, self.run_demo_button),
            ),
            (
                "Advanced SQL Verification",
                pn.Column(
                    markdown(
                        "Candidate SQL is persisted in durable runtime state. "
                        "Use synthetic data only."
                    ),
                    pn.Row(self.mode, self.metric, self.version),
                    self.time_semantics,
                    pn.Row(self.output, self.grain),
                    self.dimensions,
                    pn.Row(self.start, self.end),
                    self.sql,
                    self.verify_button,
                ),
            ),
            (
                "Local LLM Analysis",
                pn.Column(
                    self.local_question,
                    markdown(
                        "使用 Advanced SQL Verification 中已确认的分析范围。"
                        "指标歧义会先澄清；生成成功不等于验证通过。"
                    ),
                    self.button("Generate and Verify Locally", self.generate_local),
                    self.local_status,
                ),
            ),
            dynamic=False,
        )
        self.pages = pn.Tabs(
            ("Home", markdown(HELP)),
            ("New Analysis", analysis),
            (
                "Run History",
                pn.Column(
                    pn.Row(self.history_status, self.history_limit),
                    self.button("Refresh Recent Runs", self.refresh_history),
                    self.history_view,
                    self.history_run,
                    self.button("Open Selected Run", self.open_history),
                ),
            ),
            ("Run Detail", pn.Column(pn.Row(self.resume_button, self.rerun_button), self.detail)),
            (
                "System Status",
                pn.Column(
                    self.button("Check System Readiness", self.system_status), self.system_view
                ),
            ),
            dynamic=False,
            sizing_mode="stretch_width",
        )
        self.layout = pn.template.MaterialTemplate(
            title="OpsLens Console",
            header_background="#163b47",
            main=[markdown(BANNER), self.message, self.telemetry, self.pages],
        )
        self.event("SESSION_STARTED")

    def event(self, kind: EventType, *, run: bool = False, metric=None):
        ok = self.service.record_event(
            self.state.fingerprint, kind, run_id=self.state.run_id if run else None, metric=metric
        )
        if not ok:
            self.state.telemetry_degraded = True
            self.telemetry.object = (
                "Telemetry degraded — business verification outcome is unchanged."
            )

    def button(self, name, callback):
        button = pn.widgets.Button(label=name, color="primary")

        async def guarded(event):
            await self.perform(callback)

        button.on_click(guarded)
        self.buttons.append(button)
        return button

    async def perform(self, callback):
        if self.state.busy:
            return
        self.state.busy = True
        previous = [(b, b.disabled) for b in self.buttons]
        for b, _ in previous:
            b.disabled = True
        self.message.object = "RUNNING — Running verification / loading..."
        try:
            await callback()
        except Exception:
            self.message.object = "PRODUCT_ACTION_FAILED — Check inputs or contact the operator."
        finally:
            self.state.busy = False
            for b, disabled in previous:
                b.disabled = disabled
            if self.state.run_id:
                # Capability flags are updated by show_run; no database call in finally.
                self.resume_button.disabled = not getattr(self, "can_resume", False)
                self.rerun_button.disabled = not getattr(self, "can_rerun", False)

    def recipe_changed(self, event):
        try:
            recipe = self.service.load_demo_recipe(self.recipe.value)
            self.recipe_description.object = recipe.description
        except Exception:
            self.recipe_description.object = "CATALOG_UNAVAILABLE"

    def metric_changed(self, event):
        try:
            self.version.options = self.service.registry.versions(self.metric.value)
            active = self.service.registry.active(self.metric.value)
            self.version.value = active.version if active else self.version.options[-1]
            self.version_changed(None)
        except Exception:
            self.message.object = "METRIC_CONFIGURATION_UNAVAILABLE"

    def version_changed(self, event):
        definition = self.service.registry.get(self.metric.value, self.version.value)
        if definition:
            self.dimensions.value = []
            self.dimensions.options = definition.allowed_dimensions
            self.grain.value = None
            self.grain.options = {
                "No time grouping": None,
                **{g: g for g in definition.time_semantics.allowed_grains},
            }
            self.output.value = definition.metric_id
            self.time_semantics.object = (
                f"{definition.description}\nTime: {definition.time_semantics.time_column}; "
                f"{definition.time_semantics.timezone}; [start, end); "
                f"Refund policy: {definition.refund_policy}; Unit: {definition.unit}"
            )

    async def clarify(self):
        self.event("METRIC_CLARIFICATION_REQUESTED")
        decision = await self.service.clarify(self.question.value)
        self.event("METRIC_CLARIFICATION_SHOWN")
        self.clarification.objects = [safe_json(decision.model_dump(mode="json"))]
        self.metric_option.options = {
            f"{o.label} — {o.metric_id}@{o.version}": f"{o.metric_id}@{o.version}"
            for o in decision.options
        }
        self.message.object = f"{product_status(action=decision.action)} — {decision.reason_code}"

    async def lookup(self):
        result = await self.service.lookup(self.question.value)
        self.clarification.objects = [safe_json(result.model_dump(mode="json"))]
        self.message.object = "Business knowledge lookup — no analysis executed."

    async def select_metric(self):
        selected = self.metric_option.value
        if not selected:
            raise ValueError("METRIC_SELECTION_REQUIRED")
        metric, version = selected.split("@")
        self.metric.value = metric
        self.version.value = version
        self.event("METRIC_OPTION_SELECTED", metric=(metric, version))
        self.message.object = (
            "READY_TO_RUN — Metric selected only. Choose a demo or supply candidate SQL."
        )

    async def submit(self, request):
        key = self.state.submission_key(request.fingerprint())
        view = await asyncio.to_thread(self.service.start, request, idempotency_key=key)
        self.state.run_id = view.run_id
        self.event("ANALYSIS_SUBMITTED", run=True)
        self.show_run(view)

    async def run_demo(self):
        recipe = self.service.load_demo_recipe(self.recipe.value)
        self.event("DEMO_RECIPE_SELECTED")
        await self.submit(recipe.runtime_request)

    async def verify(self):
        definition = self.service.registry.get(self.metric.value, self.version.value)
        if definition is None:
            raise ValueError("METRIC_NOT_FOUND")
        intent = AnalysisIntent(
            metric_id=definition.metric_id,
            metric_version=definition.version,
            metric_output_column=self.output.value,
            time_window=TimeWindow(
                start=datetime.fromisoformat(self.start.value),
                end=datetime.fromisoformat(self.end.value),
                timezone=definition.time_semantics.timezone,
            ),
            time_grain=self.grain.value,
            dimensions=self.dimensions.value,
        )
        request = RuntimeAnalysisRequest(
            candidate_sql=self.sql.value,
            validation_mode=self.mode.value,
            intent=intent,
            metric_ref=MetricBinding(
                metric_id=definition.metric_id,
                version=definition.version,
                output_columns=[self.output.value],
            ),
        )
        await self.submit(request)

    async def generate_local(self):
        from uuid import uuid4

        from opslens.llm.provider import OllamaConfig, OllamaProvider

        config = OllamaConfig.from_env()
        definition = self.service.registry.get(self.metric.value, self.version.value)
        if definition is None:
            raise ValueError("METRIC_NOT_FOUND")
        intent = AnalysisIntent(
            metric_id=definition.metric_id,
            metric_version=definition.version,
            metric_output_column=self.output.value,
            time_window=TimeWindow(
                start=datetime.fromisoformat(self.start.value),
                end=datetime.fromisoformat(self.end.value),
                timezone=definition.time_semantics.timezone,
            ),
            dimensions=self.dimensions.value,
            time_grain=self.grain.value,
        )
        self.local_status.object = "GENERATING — Generated ≠ Verified"
        result = await self.service.analyze_local(
            self.local_question.value,
            intent,
            OllamaProvider(config),
            idempotency_key=str(uuid4()),
            max_repairs=config.max_repairs,
        )
        self.local_status.object = (
            f"{result.status} · generation attempts={len(result.attempts)} · "
            f"repairs={max(0, len(result.attempts) - 1)} · {','.join(result.reason_codes)}"
        )
        self.message.object = self.local_status.object
        if result.clarification:
            self.clarification.objects = [safe_json(result.clarification.model_dump(mode="json"))]
        if result.view:
            self.show_run(result.view)

    def show_run(self, view):
        self.state.run_id, self.state.task_id = view.run_id, view.task_id
        self.can_resume, self.can_rerun = view.can_resume, view.can_rerun
        self.event("RUN_DETAIL_OPENED", run=True)
        self.message.object = f"{view.status} — {view.headline}"
        evidence = pn.Column()
        observation = pn.Column()

        async def open_evidence():
            evidence.objects = [
                safe_json(
                    view.evidence.model_dump() if view.evidence else {"status": "UNAVAILABLE"}
                )
            ]
            self.event("EVIDENCE_OPENED", run=True)
            self.message.object = (
                "Evidence inspected — completeness and provenance are separate from validation."
            )

        async def open_observation():
            observation.objects = [
                safe_json(view.observation.model_dump(mode="json")),
                safe_json([e.model_dump(mode="json") for e in view.timeline]),
            ]
            self.event("OBSERVABILITY_OPENED", run=True)
            self.message.object = (
                "Safe observation — step timeline, attempts, retries and fingerprints."
            )

        downloads = []
        for kind, label, extension in (
            ("markdown", "Report Markdown", "md"),
            ("report", "Safe Report JSON", "json"),
            ("observation", "Safe Observation JSON", "json"),
        ):
            if kind != "observation" and view.report is None:
                continue

            def export(kind=kind, run_id=view.run_id):
                try:
                    value = self.service.export(run_id, kind)
                    self.event(
                        "REPORT_EXPORTED" if kind != "observation" else "OBSERVABILITY_OPENED",
                        run=True,
                    )
                    return io.BytesIO(value.encode())
                except Exception:
                    self.message.object = "EXPORT_FAILED — Contact the operator."
                    return io.BytesIO(b'{"code":"EXPORT_FAILED"}')

            downloads.append(
                pn.widgets.FileDownload(
                    label=label,
                    callback=export,
                    filename=f"opslens-{view.run_id}-{kind}.{extension}",
                )
            )
        feedback = []
        for label, kind in (
            ("Helpful", "FEEDBACK_HELPFUL"),
            ("Needs Correction", "FEEDBACK_NEEDS_CORRECTION"),
            ("Unclear", "FEEDBACK_UNCLEAR"),
        ):

            async def send(kind=kind):
                self.event(cast(EventType, kind), run=True)
                self.message.object = (
                    "Structured feedback recorded."
                    if not self.state.telemetry_degraded
                    else "Telemetry degraded — feedback could not be confirmed."
                )

            feedback.append(self.button(label, send))
        self.detail.objects = [
            safe_json({"generation": [g.model_dump() for g in view.generation]}),
            pn.pane.Str(
                f"{view.status}\nRun: {view.run_id}\nMetric: {view.metric_id}@{view.metric_version}"
            ),
            safe_json(
                {"reason_codes": view.reason_codes, "failure_class": view.observation.failure_class}
            ),
            markdown(render_markdown(view.report))
            if view.report
            else markdown("No Verified Fact. Report unavailable."),
            safe_json([v.model_dump(exclude={"checks"}) for v in view.report.validation_summary])
            if view.report
            else pn.Spacer(),
            self.button("Open Evidence", open_evidence),
            evidence,
            self.button("Open Observability / Event Timeline", open_observation),
            observation,
            pn.Row(*downloads),
            pn.Row(*feedback),
        ]
        self.pages.active = 3

    async def resume(self):
        if self.state.run_id is None:
            raise ValueError("RUN_REQUIRED")
        self.show_run(await asyncio.to_thread(self.service.resume, self.state.run_id))

    async def rerun(self):
        if self.state.task_id is None:
            raise ValueError("TASK_REQUIRED")
        self.event("RERUN_REQUESTED", run=True)
        self.show_run(await asyncio.to_thread(self.service.rerun, self.state.task_id))

    async def refresh_history(self):
        if self.history_limit.value is None:
            raise ValueError("LIMIT_REQUIRED")
        rows = await asyncio.to_thread(
            self.service.history,
            self.history_limit.value,
            None if self.history_status.value == "ALL" else self.history_status.value,
        )
        self.history_view.objects = [safe_json(rows)]
        self.history_run.options = [r["run_id"] for r in rows]
        self.message.object = f"Recent runs: {len(rows)} — bounded query."

    async def open_history(self):
        self.show_run(await asyncio.to_thread(self.service.get_run, self.history_run.value))

    async def system_status(self):
        result = await asyncio.to_thread(readiness, self.config)
        self.system_view.objects = [markdown(LIMITATIONS), safe_json(result)]
        self.message.object = result["code"]


def create_app(service: ProductService, config: ProductConfig | None = None):
    return Console(service, config).layout

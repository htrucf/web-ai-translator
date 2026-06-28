"""eval_adapters.py — Nối EvalPipeline (engine) vào hệ thống thật.

Hai nhóm:
  1. Judge dispatcher — gộp 2–3 đoạn vào 1 PROMPT (tiết kiệm token) + parse N điểm.
       Backend do user chọn: "off" | "web"/<vendor> | "cometkiwi".
  2. build_*_fn / run_eval_loop — bọc translate_chunk + quality heuristic + judge
       thành callable cho EvalPipeline, quản lý vòng đời browser. CriticAgent
       quyết định policy sửa: dịch → refine → đổi model → đa ứng viên → stop.

Phần thuần (build_batch_judge_prompt, parse_batch_judge_response) KHÔNG đụng
browser → test được; phần chọn model/provenance nằm ở ModelPassAgent.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Optional

from app.pdf.eval_pipeline import EvalConfig, EvalPipeline, EvalReport
from app.pdf.llm_judge import OLLAMA_URL, DEFAULT_MODEL, _compute_mqm_score
from app.pdf.quality import check_translation_quality


# ── 1. Judge gộp batch: 1 prompt cho 2–3 đoạn (token-efficient) ───────────────

def build_batch_judge_prompt(batch: list[tuple[int, str, str]]) -> str:
    """Gộp nhiều cặp (index, NGUỒN-EN, DỊCH-VI) vào 1 prompt MQM duy nhất."""
    lines = [
        "Bạn là giám khảo dịch thuật Anh→Việt theo khung MQM.",
        "Chấm TỪNG cặp (NGUỒN, DỊCH) dưới đây một cách độc lập.",
        "Chỉ trả về DUY NHẤT một mảng JSON, mỗi phần tử đúng dạng:",
        '{"index": <int>, "errors": [{"category": "...", "severity": "..."}]}',
        "category ∈ {accuracy, fluency, terminology, style, locale}; "
        "severity ∈ {minor, major, critical}. Không lỗi → errors: [].",
        "Tuyệt đối không thêm chữ nào ngoài mảng JSON.",
        "",
    ]
    for idx, src, mt in batch:
        lines.append(f"[index {idx}]")
        lines.append(f"NGUỒN (EN): {src}")
        lines.append(f"DỊCH (VI): {mt}")
        lines.append("")
    return "\n".join(lines)


def _extract_json_array(raw: str):
    s = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", s, re.S)
    if fence:
        s = fence.group(1).strip()
    start, end = s.find("["), s.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(s[start:end + 1])
    except Exception:
        return None


def parse_batch_judge_response(
    raw: str, indices: list[int]
) -> dict[int, Optional[float]]:
    """Parse mảng JSON → {index: MQM}. Index thiếu/hỏng → None.

    MQM tính lại từ `errors` (authoritative, giống các judge đơn lẻ); nếu phần
    tử không có `errors` thì lấy `score` thô.
    """
    result: dict[int, Optional[float]] = {i: None for i in indices}
    arr = _extract_json_array(raw)
    if not isinstance(arr, list):
        return result
    for item in arr:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if idx not in result:
            continue
        errors = item.get("errors")
        if isinstance(errors, list):
            result[idx] = round(_compute_mqm_score(errors), 1)
        elif isinstance(item.get("score"), (int, float)):
            result[idx] = float(item["score"])
    return result


# ── 2b. Judge backend dispatcher (user chọn) ──────────────────────────────────

def make_ollama_judge_fn(model: str | None = None, timeout: float = 240.0):
    """Judge gộp-prompt qua Ollama (local, ≠ mọi web translator). Không browser."""
    import httpx
    mdl = model or DEFAULT_MODEL

    async def judge_fn(batch):
        prompt = build_batch_judge_prompt(batch)
        indices = [i for i, _, _ in batch]

        def _call():
            r = httpx.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": mdl, "prompt": prompt, "stream": False,
                      "options": {"temperature": 0.1, "num_predict": 2048}},
                timeout=timeout,
            )
            return r.json().get("response", "") if r.is_success else ""

        raw = await asyncio.to_thread(_call)   # không block event loop
        return parse_batch_judge_response(raw, indices)

    return judge_fn, None


def make_web_judge_fn(judge_backend: str):
    """Judge gộp-prompt qua web AI `judge_backend` (đã resolve ≠ model dịch).

    Mở 1 session browser riêng (chạy song song với các tab dịch). Lock để
    các batch judge tuần tự trên cùng session.
    """
    state: dict = {"tr": None, "page": None}
    lock = asyncio.Lock()

    async def _ensure():
        if state["page"] is None:
            from app.services.translator import WebAITranslator
            tr = WebAITranslator(backend=judge_backend)
            _ctx, page = await tr.launch_browser()
            state["tr"], state["page"] = tr, page
        return state["tr"], state["page"]

    async def judge_fn(batch):
        indices = [i for i, _, _ in batch]
        prompt = build_batch_judge_prompt(batch)
        async with lock:
            tr, page = await _ensure()
            raw = await tr._send_prompt_and_get_response(page, prompt)
        return parse_batch_judge_response(raw, indices)

    async def cleanup():
        if state["tr"] is not None:
            try:
                await state["tr"].cleanup()
            except Exception:
                pass

    return judge_fn, cleanup


def make_judge_fn(judge_backend: str | None, translator_models: list[str]):
    """Trả (judge_fn, cleanup) theo lựa chọn user. None → tắt judge."""
    jb = (judge_backend or "").lower().strip()
    if jb in ("", "off", "none"):
        return None, None
    if jb == "ollama":
        return make_ollama_judge_fn()
    primary = (translator_models[0] if translator_models else "gemini")
    from app.pdf.agents.judge_agent import JudgeAgent
    resolved_judge_backend = judge_backend
    if jb == "web":
        resolved_judge_backend = next(
            (m for m in translator_models if m and m != primary),
            "web",
        )
    agent = JudgeAgent(judge_backend=resolved_judge_backend)

    async def judge_fn(batch):
        return await agent.judge_batch(batch, translator_backend=primary)

    return judge_fn, agent.cleanup


# ── 3. Callable builders (glue browser) ───────────────────────────────────────

def _ensure_page_fn(page):
    async def _f():
        return page
    return _f


def _make_chunk_translator(ctx, translators: dict):
    """Trả về translate_one(model, idx) -> str — dịch 1 chunk trên tab của `model`.

    KHÔNG tự giữ lock: caller phải bọc trong locks[model]. Tách ra vì lock của
    asyncio không reentrant — refine/ensemble cũng cần cùng tab, nếu translate
    tự lock rồi caller lock lại sẽ deadlock. Trả "" nếu thất bại.
    """
    from app.pdf.agents.base import AgentContext
    from app.pdf.agents.cross_model_agreement_agent import CrossModelAgreementAgent
    from app.pdf.agents.translator_agent import TranslateRequest, TranslatorAgent

    agent = TranslatorAgent()
    base_style_anchor = ctx.progress.get("style_anchor")

    async def translate_one(model: str, idx: int) -> str:
        tr, page = translators[model]
        handoff = CrossModelAgreementAgent.get_handoff_anchor(ctx, idx, model)
        style_anchor = CrossModelAgreementAgent.merge_style_anchor(
            base_style_anchor, handoff
        )
        section_hint = ""
        if ctx.plan is not None:
            sec = ctx.plan.section_for_chunk(idx)
            if sec:
                section_hint = sec.title
        wctx = AgentContext(
            job_id=ctx.job_id, job_dir=ctx.job_dir, pdf_path=ctx.pdf_path,
            mode=ctx.mode, blocks=ctx.blocks, chunks=ctx.chunks, plan=ctx.plan,
            glossary=ctx.glossary, glossary_enabled=ctx.glossary_enabled,
            locked_terms=ctx.locked_terms, memory=None, translator=tr,
            page=page, context=None, progress=ctx.progress,
            save_progress=ctx.save_progress, is_cancelled=ctx.is_cancelled,
            ensure_page=_ensure_page_fn(page), settings=ctx.settings,
        )
        req = TranslateRequest(
            chunk_index=idx, chunk=ctx.chunks[idx], section_hint=section_hint,
            max_retries=ctx.settings.get("max_retries", 2),
            base_backoff=ctx.settings.get("base_backoff", 5),
            style_anchor=style_anchor, anti_hallucination=True, worker_page=page,
        )
        result = await agent.translate_chunk(wctx, req)
        data = result.data or {}
        text = data.get("translated", "")
        return text if (result.success and text) else ""

    return translate_one


def build_translate_fn(ctx, scheduler, translators: dict, locks: dict):
    """translate_fn(idx) → (text, ok). Chọn model qua ModelPassAgent scheduler.

    Hàm này giữ cho tương thích; eval-loop hiện dùng `build_produce_fn`.
    """
    translate_one = _make_chunk_translator(ctx, translators)

    async def translate_fn(idx: int):
        plan = scheduler.next_attempt(idx)
        model = plan.model
        async with locks[model]:
            text = await translate_one(model, idx)
        return text, bool(text)

    return translate_fn


def build_heuristic_fn(ctx, errors_by_idx: dict | None = None, codec=None):
    """heuristic_fn(idx, text) → điểm 0..100 — Gate 1 qua ``codec.evaluate``.

    Áp text vào chunk (codec.apply) rồi chấm; gom error-list cho Critic-hub vào
    ``errors_by_idx[idx]``. codec=None → PdfEvalCodec (PDF, hành vi như cũ:
    LocalJudge cấu trúc + GlossaryJudge thuật ngữ).
    """
    if codec is None:
        from app.pdf.eval_codec import PdfEvalCodec
        codec = PdfEvalCodec()
    glossary = ctx.glossary if ctx.glossary_enabled else None

    def heuristic_fn(idx: int, text: str) -> float:
        chunk = ctx.chunks[idx]
        codec.apply(text, chunk)
        score, errors = codec.evaluate(chunk, glossary)
        if errors_by_idx is not None:
            errors_by_idx[idx] = errors
        return score

    return heuristic_fn


def make_generic_translate_factory(codec):
    """Factory dịch GENERIC dùng codec — cho Office/LaTeX (không qua TranslatorAgent).

    codec cần có thêm `translate_prompt(src)->str` và `extract(raw)->str`.
    Trả về factory(ctx, translators) → translate_one(model, idx) đúng kiểu mà
    build_produce_fn mong đợi. Lock per-model do build_produce_fn quản (không
    lock ở đây).
    """
    def factory(ctx, translators):
        async def translate_one(model: str, idx: int) -> str:
            tr, page = translators[model]
            src = codec.to_source_text(ctx.chunks[idx])
            prompt = codec.translate_prompt(src)
            try:
                raw = await tr._send_prompt_and_get_response(page, prompt)
            except Exception:
                return ""
            return codec.extract(raw)
        return translate_one
    return factory


def build_produce_fn(ctx, models: list[str], translators: dict, locks: dict,
                     heuristic_fn, errors_by_idx: dict | None = None, codec=None,
                     translate_one_factory=None, attempt_scheduler=None,
                     ensure_model=None):
    """produce_fn(idx) → (text, ok) — thực thi policy sửa từ CriticAgent.

    EvalPipeline gọi produce_fn(idx) đúng 1 lần mỗi attempt và vẫn chỉ lo
    queue/concurrency. ModelPassAgent cấp model/provenance theo preference user;
    CriticAgent quyết định attempt đó là dịch mới, refine, đổi model, ensemble
    hay stop. Adapter này chỉ thực thi quyết định đó.

    Mọi thao tác trên 1 model bọc trong locks[model] (không reentrant). Ensemble
    gather 2 model khác lock → song song thật; nếu chỉ 1 model thì 2 nhánh tự
    tuần tự hoá qua cùng lock (vẫn an toàn).
    """
    from app.pdf.agents.cross_model_agreement_agent import CrossModelAgreementAgent
    from app.pdf.agents.critic_agent import CriticAgent
    from app.pdf.agents.model_pass_agent import FAILOVER_THRESHOLD, ModelPassAgent

    if codec is None:
        from app.pdf.eval_codec import PdfEvalCodec
        codec = PdfEvalCodec()
    translate_one = (translate_one_factory or _make_chunk_translator)(ctx, translators)
    critic = CriticAgent()
    cross_model = CrossModelAgreementAgent()
    active_glossary = ctx.glossary if ctx.glossary_enabled else {}
    scheduler = attempt_scheduler or ModelPassAgent.create_attempt_scheduler(models)
    provenance_lock = asyncio.Lock()
    health_lock = asyncio.Lock()
    fail_counts: dict[str, int] = {}

    def _previous_model(idx: int, fallback: str) -> str:
        attempts = ctx.progress.get("translation_attempts", {}).get(str(idx), [])
        for rec in reversed(attempts):
            model = rec.get("selected_model") or rec.get("model")
            if model and model != fallback:
                return model
        return models[0] if models else fallback

    def _prepare_handoff(idx: int, to_model: str):
        from_model = _previous_model(idx, to_model)
        cross_model.prepare_handoff(
            ctx,
            idx,
            from_model=from_model,
            to_model=to_model,
            codec=codec,
        )

    async def _locked_translate(model: str, idx: int) -> str:
        if ensure_model is not None and not await ensure_model(model):
            await _record_model_health(
                model, False, reason="model_launch_failed", idx=idx
            )
            return ""
        async with locks[model]:
            text = await translate_one(model, idx)
        await _record_model_health(
            model, bool(text), reason="translate_empty", idx=idx
        )
        return text

    async def _refine(model: str, idx: int) -> str:
        if ensure_model is not None and not await ensure_model(model):
            await _record_model_health(
                model, False, reason="model_launch_failed", idx=idx
            )
            return ""
        tr, page = translators[model]
        errs = errors_by_idx.get(idx) if errors_by_idx else None
        async with locks[model]:
            text = await critic.refine_chunk(
                ctx.chunks[idx], page=page, translator=tr,
                glossary=active_glossary, locked_terms=ctx.locked_terms,
                errors=errs, codec=codec,
            )
        await _record_model_health(
            model, bool(text), reason="refine_empty", idx=idx
        )
        return text

    async def _record_model_health(
        model: str,
        ok: bool,
        *,
        reason: str,
        idx: int | None = None,
    ):
        async with health_lock:
            health = ctx.progress.setdefault("model_health", {})
            entry = health.setdefault(model, {
                "consecutive_failures": 0,
                "down": False,
                "reason": "",
            })
            if ok:
                fail_counts[model] = 0
                entry.update({
                    "consecutive_failures": 0,
                    "down": scheduler.is_model_down(model),
                    "reason": entry.get("reason", ""),
                })
                return

            count = fail_counts.get(model, 0) + 1
            fail_counts[model] = count
            entry.update({
                "consecutive_failures": count,
                "last_failure": reason,
            })
            if count >= FAILOVER_THRESHOLD and not scheduler.is_model_down(model):
                scheduler.mark_model_down(model, reason=reason)
                available = scheduler.available_models()
                to_model = available[0] if available else ""
                from_model = cross_model.current_style_owner(ctx) or model
                handoff = None
                if idx is not None and to_model:
                    handoff = cross_model.prepare_global_handoff(
                        ctx,
                        idx,
                        from_model=from_model,
                        to_model=to_model,
                        reason=reason,
                        codec=codec,
                    )
                entry.update({
                    "down": True,
                    "reason": reason,
                    "threshold": FAILOVER_THRESHOLD,
                })
                event = {
                    "from_model": model,
                    "style_from_model": from_model,
                    "to_model": to_model,
                    "reason": reason,
                    "threshold": FAILOVER_THRESHOLD,
                    "available_models": available,
                    "unavailable_models": scheduler.unavailable_models(),
                    "handoff_created": bool(handoff),
                }
                ctx.progress["model_failover"] = event
                ctx.progress.setdefault("model_failover_history", []).append(event)
                save = getattr(ctx, "save_progress", None)
                if callable(save):
                    try:
                        save()
                    except Exception:
                        pass

    async def _record_attempt(
        plan,
        decision,
        text: str,
        selected_model: str | None,
    ):
        record = plan.provenance(selected_model=selected_model)
        record.update(decision.provenance())
        record.update({
            "ok": bool(text),
            "text_chars": len(text or ""),
        })
        async with provenance_lock:
            attempts = ctx.progress.setdefault("translation_attempts", {})
            attempts.setdefault(str(plan.chunk_index), []).append(record)
            if text:
                cross_model.record_success(
                    ctx, plan.chunk_index, model=selected_model or plan.model
                )
                latest = ctx.progress.setdefault("translation_provenance", {})
                latest[str(plan.chunk_index)] = record
            save = getattr(ctx, "save_progress", None)
            if callable(save):
                try:
                    save()
                except Exception:
                    pass

    async def produce_fn(idx: int):
        if ctx.is_cancelled():
            return "", False
        plan = scheduler.next_attempt(idx)
        errs = errors_by_idx.get(idx) if errors_by_idx else None
        decision = critic.decide_repair(plan, errors=errs)
        selected_model = decision.model

        if decision.should_stop:
            text = ""
        elif decision.action == "translate":
            text = await _locked_translate(decision.model, idx)
        elif decision.action == "refine":
            text = await _refine(decision.model, idx)
            if not text:                       # refine fail → vẫn thử dịch lại m0
                text = await _locked_translate(decision.model, idx)
        elif decision.action == "change_model":
            _prepare_handoff(idx, decision.model)
            text = await _locked_translate(decision.model, idx)
        elif decision.action == "ensemble":
            for model in decision.candidate_models:
                _prepare_handoff(idx, model)
            results = await asyncio.gather(
                *[
                    _locked_translate(model, idx)
                    for model in decision.candidate_models
                ],
                return_exceptions=True,
            )
            cands = [
                (model, text)
                for model, text in zip(decision.candidate_models, results)
                if isinstance(text, str) and text
            ]
            if not cands:
                text = ""
            elif len(cands) == 1:
                selected_model, text = cands[0]
            else:
                selected_model, text = max(
                    cands, key=lambda item: heuristic_fn(idx, item[1])
                )
        else:
            text = ""
        if ctx.is_cancelled():
            return "", False
        await _record_attempt(plan, decision, text, selected_model)
        return text, bool(text)

    return produce_fn


async def run_eval_loop(
    ctx,
    models: list[str],
    judge_backend: str | None = "web",
    config: EvalConfig | None = None,
    codec=None,
    translate_one_factory=None,
) -> EvalReport:
    """Chạy vòng dịch ∥ đánh giá end-to-end, ghi best-so-far vào progress.

    `codec` (EvalCodec) cấp các thao tác phụ-thuộc-định-dạng (render/apply/chấm).
    None → PdfEvalCodec (PDF). Office/LaTeX truyền codec riêng để dùng CÙNG lõi.

    Quản lý: mở 1 session/model, build callable, chạy EvalPipeline, áp bản dịch
    tốt nhất vào chunk + progress["translated_chunks"], lưu report, dọn browser.
    """
    from app.services.translator import WebAITranslator

    if codec is None:
        from app.pdf.eval_codec import PdfEvalCodec
        codec = PdfEvalCodec()

    from app.pdf.agents.model_pass_agent import ModelPassAgent

    requested_models = ModelPassAgent.create_attempt_scheduler(models).models
    translators: dict = {}
    locks: dict = {m: asyncio.Lock() for m in requested_models}
    launch_locks: dict = {m: asyncio.Lock() for m in requested_models}
    launch_errors: dict[str, str] = {}

    async def ensure_model(model: str) -> bool:
        model = (model or "").strip().lower()
        if not model:
            return False
        if model in translators:
            return True
        if model not in locks:
            locks[model] = asyncio.Lock()
            launch_locks[model] = asyncio.Lock()
        async with launch_locks[model]:
            if model in translators:
                return True
            # Resilient: chỉ mở model khi thật sự cần. Model lỗi sẽ bị
            # scheduler đánh dấu down sau vài attempt, rồi mới chuyển model.
            try:
                tr = WebAITranslator(backend=model)
                _c, page = await tr.launch_browser()
                translators[model] = (tr, page)
                print(f"[eval-loop] launched model '{model}'")
                return True
            except Exception as e:
                msg = str(e)
                launch_errors[model] = msg
                print(f"[eval-loop] launch model '{model}' failed, skipping: {msg}")
                return False

    judge_fn, judge_cleanup = make_judge_fn(judge_backend, requested_models)

    # errors_by_idx: Critic-hub gom lỗi từ panel judge (Local + Glossary) per-chunk
    # để refine sửa đúng chỗ thay vì tự tính lại.
    errors_by_idx: dict = {}
    heuristic_fn = build_heuristic_fn(ctx, errors_by_idx, codec)
    attempt_scheduler = ModelPassAgent.create_attempt_scheduler(requested_models)
    produce_fn = build_produce_fn(ctx, requested_models, translators, locks, heuristic_fn,
                                  errors_by_idx, codec, translate_one_factory,
                                  attempt_scheduler, ensure_model)

    def source_fn(i: int) -> str:
        return codec.to_source_text(ctx.chunks[i])

    cfg = config or EvalConfig(num_workers=max(2, len(requested_models)))

    try:
        def on_chunk_finalized(idx: int, status: str, stage: str, score: float, state):
            text = getattr(state, "best_text", "") or getattr(state, "text", "")
            if text:
                translated = ctx.progress.setdefault("translated_chunks", {})
                translated[str(idx)] = text
                try:
                    codec.apply(text, ctx.chunks[idx])
                except Exception:
                    pass

            translated_count = len([
                v for v in (ctx.progress.get("translated_chunks") or {}).values()
                if v
            ])
            total = len(ctx.chunks)
            live = ctx.progress.setdefault("eval_loop_live", {})
            passed = set(live.get("passed") or [])
            flagged = set(live.get("flagged") or [])
            if status == "passed":
                passed.add(idx)
                flagged.discard(idx)
            elif status == "flagged":
                flagged.add(idx)
                passed.discard(idx)
            live.update({
                "passed": sorted(passed),
                "flagged": sorted(flagged),
                "last_finalized_chunk": idx,
                "last_stage": stage,
                "last_score": round(score, 1),
            })
            ctx.progress["current_chunk"] = translated_count
            ctx.progress["total_chunks"] = total
            ctx.progress["phase"] = "eval_loop"
            ctx.progress["status"] = (
                f"eval-loop: {translated_count}/{total} chunks finalized "
                f"({len(passed)} passed, {len(flagged)} flagged)"
            )
            save = getattr(ctx, "save_progress", None)
            if callable(save):
                save()

        pipe = EvalPipeline(
            list(range(len(ctx.chunks))),
            produce_fn, heuristic_fn, source_fn, judge_fn, cfg,
            is_cancelled=ctx.is_cancelled,
            progress_fn=on_chunk_finalized,
        )
        report = await pipe.run()

        finals = pipe.final_translations()
        translated = ctx.progress.setdefault("translated_chunks", {})
        for idx, text in finals.items():
            if not text:
                continue
            translated[str(idx)] = text
            try:
                codec.apply(text, ctx.chunks[idx])
            except Exception:
                pass
        await _discover_glossary_terms_from_finals(
            ctx, finals, translators, locks, codec
        )
        report_dict = report.to_dict()
        report_dict["provenance"] = ctx.progress.get("translation_provenance", {})
        ctx.progress["eval_loop"] = report_dict
        ctx.save_progress()
        return report
    finally:
        for tr, _p in translators.values():
            try:
                await tr.cleanup()
            except Exception:
                pass
        if judge_cleanup:
            try:
                await judge_cleanup()
            except Exception:
                pass


async def _discover_glossary_terms_from_finals(ctx, finals, translators, locks, codec):
    """Bồi đắp glossary bằng prompt so sánh nguồn/dịch sau khi có bản dịch tốt."""
    if not finals or not getattr(ctx, "glossary_enabled", True) or not translators:
        return
    try:
        from app.pdf.glossary import (
            extract_new_terms_prompt,
            merge_glossary,
            parse_extraction_response,
        )
    except Exception:
        return

    glossary_state = ctx.progress.setdefault(
        "glossary",
        {"terms": {}, "enabled": True, "locked": [], "fields": {}},
    )
    terms = glossary_state.setdefault("terms", {})
    checked = {str(x) for x in glossary_state.get("discovery_checked_chunks", [])}
    model = next(iter(translators.keys()))
    tr, page = translators[model]
    lock = locks.get(model) or asyncio.Lock()
    added_total = 0
    checked_now = 0

    for idx, translated in sorted(finals.items()):
        key = str(idx)
        if checked_now >= 5:
            break
        if key in checked or not translated:
            continue
        try:
            original = codec.to_source_text(ctx.chunks[idx])
            prompt = extract_new_terms_prompt(original, translated)
            async with lock:
                raw = await tr._send_prompt_and_get_response(page, prompt)
            new_terms = parse_extraction_response(raw)
            truly_new = {k: v for k, v in new_terms.items() if k not in terms}
            if truly_new:
                terms = merge_glossary(terms, truly_new)
                glossary_state["terms"] = terms
                added_total += len(truly_new)
        except Exception as e:
            print(f"[eval-loop] glossary discovery failed @ chunk {idx}: {e}")
        checked.add(key)
        checked_now += 1

    glossary_state["discovery_checked_chunks"] = sorted(
        checked,
        key=lambda x: int(x) if str(x).isdigit() else 999999,
    )
    if added_total:
        ctx.glossary = terms
        print(f"[eval-loop] glossary discovery: +{added_total} terms")

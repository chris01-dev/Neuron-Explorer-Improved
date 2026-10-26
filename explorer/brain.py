"""The steering brain: a small local agent model (default: the Gemma-4 12B agentic tune, 4-bit) that turns a
natural-language wish into a verified recipe of neuron pushes for the steered chat model.

Loop: decompose the wish into word-stems -> lens_search -> assemble a blend of doses -> test it live
(up / neutral / down) -> critique -> adjust -> finish. Everything streams to the UI as NDJSON events.

    EXPLORER_BRAIN_MODEL  model id or local path (default: the HF repo; downloads on first use)
    EXPLORER_BRAIN_4BIT   "0" to disable 4-bit NF4 and load bf16 (needs ~24 GB VRAM for the 12B)
"""
import json
import os
import re
import threading

import numpy as np
import torch
from transformers import AutoModelForMultimodalLM, AutoTokenizer, BitsAndBytesConfig

BRAIN_MODEL = os.environ.get("EXPLORER_BRAIN_MODEL",
                             "yuxinlu1/gemma-4-12B-agentic-fable5-composer2.5-v2-3.5x-tau2")
FOUR_BIT = os.environ.get("EXPLORER_BRAIN_4BIT", "1") != "0"
MAX_ROUNDS = 14
MAX_NEW_TOKENS = 900

SYSTEM_PROMPT = """You are the steering brain of a neuron explorer application. Your ONLY job: turn the user's wish into a verified recipe of neuron activations (layer, n, dose) that makes the Qwen3.5-4B chat model behave as asked.

Tools:
- lens_search(query): neurons whose lens contains the query word. An asterisk means the direction was measured to genuinely steer those words - prefer those. Use short word stems, one concept per call.
- add_neuron(layer, n, dose): add one push to the recipe. Dose sense: 0.15-0.3 gentle support, 0.3-0.5 one strong lead concept, above 0.8 usually collapses replies into one repeated word. Negative dose pushes the other way. Do not round the numbers.
- remove_neuron(layer, n): remove it from the recipe.
- test_steering(prompt): run the current recipe on the live model; returns UP (recipe applied), NEUTRAL (unsteered, the control), DOWN (recipe reversed).
- finish(summary): done; summary is one sentence for the user.

Method, at most 12 rounds of tool calls (you may put SEVERAL tool calls in one round - batch your lens_search calls together):
1. First say which 2-5 word stems capture the wish (personality words, its vocabulary, topics it should bring up).
2. lens_search your stems in one batched round. Assemble a draft recipe of 2-6 neurons across DIFFERENT concepts by round 3. The LEAD neuron should be a strong verified concept writer from a HIGH layer (20-31) at 0.3-0.5: high layers write words directly. Supports at 0.15-0.3, any layer. Two strong neurons writing the same word multiply each other and collapse into a word-loop: do not do that.
3. test_steering with a short natural prompt by round 4 - stop searching once you have a draft; testing tells you more than more searching.
4. Critique the UP reply against the wish precisely. Too subtle -> raise the biggest dose by about 1.3x or add another concept neuron. One repeated word -> lower the biggest dose. Wrong flavour -> remove that neuron and lens_search a different stem.
5. finish as soon as UP clearly shows the requested behaviour.
Only use neuron addresses that lens_search returned. Never invent addresses, never skip testing, keep NEUTRAL intact."""

TOOLS = [
    {"type": "function", "function": {"name": "lens_search",
      "description": "find neurons whose lens contains a word; returns layer, n and the words each pushes",
      "parameters": {"type": "object",
                     "properties": {"query": {"type": "string", "description": "a single word or word stem"},
                                    "limit": {"type": "string", "description": "max results, default 8"}},
                     "required": ["query"]}}},
    {"type": "function", "function": {"name": "add_neuron",
      "description": "add a neuron push to the recipe",
      "parameters": {"type": "object",
                     "properties": {"layer": {"type": "string"}, "n": {"type": "string"},
                                    "dose": {"type": "string", "description": "signed number, e.g. 0.35 or -0.2"}},
                     "required": ["layer", "n", "dose"]}}},
    {"type": "function", "function": {"name": "remove_neuron",
      "description": "remove a neuron from the recipe",
      "parameters": {"type": "object",
                     "properties": {"layer": {"type": "string"}, "n": {"type": "string"}},
                     "required": ["layer", "n"]}}},
    {"type": "function", "function": {"name": "test_steering",
      "description": "run the current recipe through the live model and return the three replies",
      "parameters": {"type": "object",
                     "properties": {"prompt": {"type": "string", "description": "short natural prompt"}},
                     "required": []}}},
    {"type": "function", "function": {"name": "finish",
      "description": "call when the recipe is verified; summary is one sentence for the user",
      "parameters": {"type": "object",
                     "properties": {"summary": {"type": "string"}},
                     "required": ["summary"]}}},
]

CALL_RE = re.compile(r"<\|tool_call>call:([a-zA-Z_]+)\{(.*?)\}<tool_call\|>", re.DOTALL)
ARG_RE = re.compile(r"([a-zA-Z_]+)\s*:\s*(?:<\|\"\|>)?(.*?)(?:<\|\"\|>)?(?=\s*,\s*[a-zA-Z_]+\s*:|$)", re.DOTALL)
THOUGHT_RE = re.compile(r"<\|channel>thought\s*\n(.*?)\n?<(?:channel\|)>", re.DOTALL)


def _parse_args(s):
    out = {}
    for m in ARG_RE.finditer(s):
        key, val = m.group(1), m.group(2).strip().strip(",").strip()
        if val and key not in out:
            out[key] = val
    return out


class Brain:
    def __init__(self, rt):
        self.rt = rt
        self.model = self.tok = None
        self.dev = "cuda" if torch.cuda.is_available() else "cpu"
        self.status = "idle"
        self._lock = threading.Lock()

    # ---------- loading ----------
    def ensure_loaded(self, emit):
        if self.model is not None:
            return
        with self._lock:
            if self.model is not None:
                return
            self.status = "loading"
            emit({"t": "status", "text": f"loading the brain model ({'4-bit, about a minute' if FOUR_BIT else 'bf16'}; once)"})
            kw = {"dtype": torch.bfloat16}
            if FOUR_BIT and self.dev == "cuda":
                kw["quantization_config"] = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                                                              bnb_4bit_compute_dtype=torch.bfloat16)
                kw["device_map"] = {"": 0}
            self.tok = AutoTokenizer.from_pretrained(BRAIN_MODEL)
            m = AutoModelForMultimodalLM.from_pretrained(BRAIN_MODEL, **kw)
            if kw.get("device_map") is None:
                m = m.to(self.dev)
            self.model = m.eval()
            self.status = "ready"
            emit({"t": "status", "text": "brain ready"})

    # ---------- tools (direct access to the explorer runtime) ----------
    def _lens_search(self, args):
        q = str(args.get("query", "")).strip().lower()
        limit = 8
        try:
            limit = max(2, min(16, int(float(args.get("limit", 8)))))
        except (TypeError, ValueError):
            pass
        if len(q) < 2:
            return "query too short: give one word or stem"
        vocab = self.rt.vocab_strings()
        idx, st = self.rt.lens_index(), self.rt.steer_index()
        matching = np.array([i for i, t in enumerate(vocab) if q in str(t).lower()], dtype=np.int32)
        if not len(matching):
            return f'no lens token contains "{q}" - try a different stem'
        hits = {}
        for src, mark, arrs in (("lens", False, idx), ("steer", True, st)):
            if arrs is None:
                continue
            arr = arrs["up"]  # pushing UP; the standard buttons only steer the up direction
            hit = np.isin(arr, matching)
            count, best = hit.sum(-1), np.where(hit, np.arange(arr.shape[-1]), 99).min(-1)
            for l, n in zip(*np.nonzero(count)):
                key = (int(l), int(n))
                cur = hits.get(key)
                if cur is None or best[l, n] < cur["rank"]:
                    hits[key] = {"rank": int(best[l, n]), "verified": mark or (cur or {}).get("verified", False),
                                 "lens": " ".join(str(vocab[t]) for t in arr[int(l), int(n), :6])}
        rows = sorted(hits.items(), key=lambda kv: (not kv[1]["verified"], kv[1]["rank"]))[:limit]
        out = [f"L{l} N{n} {'[verified]' if v['verified'] else ''} {v['lens']}" for (l, n), v in rows]
        return "\n".join(out) if out else f'no up-direction lens token contains "{q}"'

    def _test_steering(self, args, blend, emit):
        prompt = str(args.get("prompt", "")).strip() or "Who are you?"
        if not blend:
            return "the recipe is empty: add_neuron first", ""
        pushes = [(b["layer"], b["n"], b["dose"], "add") for b in blend]
        texts = ["", "", ""]
        for row, delta in self.rt.steer_multi(pushes, [[{"role": "user", "content": prompt}]] * 3, 130):
            if row is not None:
                texts[row] += delta
        up = self._cut(texts[0], 420)
        zero = self._cut(texts[1], 260)
        down = self._cut(texts[2], 260)
        emit({"t": "trial", "prompt": prompt, "up": up, "zero": zero, "down": down})
        return f"UP: {up}\nNEUTRAL: {zero}\nDOWN: {down}", up

    @staticmethod
    def _cut(text, n):
        text = " ".join(str(text).split())
        return text if len(text) <= n else text[:n] + " …"

    def _exec(self, name, args, blend, emit):
        """Runs one tool; returns the result string (and mutates blend)."""
        if name == "lens_search":
            return self._lens_search(args)
        if name == "finish":
            return str(args.get("summary", "recipe ready"))
        if name not in ("add_neuron", "remove_neuron", "test_steering"):
            return f"unknown tool {name} - only lens_search, add_neuron, remove_neuron, test_steering and finish exist here"
        if name == "test_steering":
            res, _ = self._test_steering(args, blend, emit)
            return res
        def num(key, default=None):
            s = str(args.get(key, default))
            m = re.search(r"-?\d+(?:\.\d+)?", s)
            return float(m.group(0)) if m else default
        layer, n = int(num("layer", -1)), int(num("n", -1))
        if not (0 <= layer < len(self.rt.lm.layers)):
            return f"layer {layer} does not exist in this model - use only addresses from lens_search"
        inter = self.rt.lm.layers[layer].mlp.down_proj.in_features
        if not (0 <= n < inter):
            return f"neuron {n} does not exist in layer {layer}"
        if name == "add_neuron":
            dose = num("dose", 0.25)
            if dose is None:
                return "dose must be a number like 0.35"
            dose = max(-3.0, min(3.0, dose))
            for b in blend:
                if b["layer"] == layer and b["n"] == n:
                    b["dose"] = dose
                    return f"updated L{layer} N{n} to {dose}"
            blend.append({"layer": layer, "n": n, "dose": dose})
            return f"added L{layer} N{n} dose {dose}; recipe now has {len(blend)} neurons"
        if name == "remove_neuron":
            blend[:] = [b for b in blend if not (b["layer"] == layer and b["n"] == n)]
            return f"removed L{layer} N{n}; recipe now has {len(blend)} neurons"
        if name == "test_steering":
            res, _ = self._test_steering(args, blend, emit)
            return res
        return f"unknown tool {name} - use lens_search, add_neuron, remove_neuron, test_steering or finish"

    # ---------- the loop ----------
    def _generate(self, msgs):
        enc = self.tok.apply_chat_template(msgs, tools=TOOLS, add_generation_prompt=True,
                                           enable_thinking=True, tokenize=True, return_tensors="pt")
        ids = enc["input_ids"] if hasattr(enc, "keys") else enc
        ids = ids.to(self.dev)
        with torch.inference_mode():
            out = self.model.generate(ids, max_new_tokens=MAX_NEW_TOKENS, do_sample=True,
                                      temperature=0.85, top_p=0.95, top_k=64,
                                      repetition_penalty=1.05, pad_token_id=self.tok.pad_token_id)
        text = self.tok.decode(out[0][ids.shape[-1]:], skip_special_tokens=False)
        return text.split("<turn|>")[0].split("<eos>")[0]

    def run(self, goal, emit):
        if self._lock.locked() and self.model is None:
            emit({"t": "error", "error": "the brain is still loading"}); return
        self.rt.ensure_loaded()
        self.ensure_loaded(emit)
        if self.status == "busy":
            emit({"t": "error", "error": "a brain run is already in progress"}); return
        self.status = "busy"
        try:
            msgs = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": str(goal)}]
            blend = []
            summary = "recipe ready"
            for i in range(MAX_ROUNDS):
                emit({"t": "round", "i": i + 1})
                text = self._generate(msgs)
                th = THOUGHT_RE.search(text)
                if th:
                    emit({"t": "thought", "text": " ".join(th.group(1).split())[:600]})
                calls = [(m.group(1), _parse_args(m.group(2))) for m in CALL_RE.finditer(text)]
                if not calls:
                    plain = " ".join(text.split())[:400]
                    emit({"t": "done", "summary": plain or "no plan produced - try rephrasing the wish"})
                    return
                msgs.append({"role": "assistant", "content": "", "tool_calls":
                             [{"function": {"name": nm, "arguments": {k: v for k, v in a.items()}}} for nm, a in calls]})
                for nm, a in calls:
                    emit({"t": "tool", "name": nm, "input": a})
                    if nm == "finish":
                        summary = self._exec(nm, a, blend, emit)
                        emit({"t": "blend", "blend": blend.copy()})
                        emit({"t": "done", "summary": summary})
                        return
                    res = self._exec(nm, a, blend, emit)
                    emit({"t": "result", "name": nm, "text": self._cut(res, 400)})
                    msgs.append({"role": "tool", "name": nm, "content": self._cut(res, 900)})
                emit({"t": "blend", "blend": blend.copy()})
            emit({"t": "blend", "blend": blend.copy()})
            summary = summary + " (stopped at the round cap)"
            if blend:
                _, up = self._test_steering({"prompt": "Who are you?"}, blend, emit)
                summary = f"recipe handed over (stopped at the round cap); final check ▲: {self._cut(up, 260)}"
            emit({"t": "done", "summary": summary})
        finally:
            self.status = "ready"

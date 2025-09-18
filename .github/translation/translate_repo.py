# .github/translation/translate_repo.py
from __future__ import annotations
import os, re, json, time, csv, sys
from pathlib import Path
from typing import Any, Dict, List, Tuple, Iterable

import yaml
import requests

ROOT = Path(".").resolve()
CFG_PATH = ROOT / ".github" / "translation" / "translate.config.yml"
GLOSSARY_PATH = ROOT / ".github" / "translation" / "glossary_ja_en.csv"

# ---------------------------
# Utilities
# ---------------------------

def load_yaml(p: Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8"))

def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")

def write_text(p: Path, s: str) -> None:
    p.write_text(s, encoding="utf-8")

def load_glossary(p: Path) -> List[Tuple[str,str]]:
    if not p.exists():
        return []
    rows: List[Tuple[str,str]] = []
    with p.open("r", encoding="utf-8", newline="") as f:
        for src, dst, *rest in csv.reader(f):
            if src and dst:
                rows.append((src, dst))
    # 長い語句を先に置換（衝突回避）
    rows.sort(key=lambda x: len(x[0]), reverse=True)
    return rows

def apply_glossary(s: str, glossary: List[Tuple[str,str]]) -> str:
    for src, dst in glossary:
        s = s.replace(src, dst)
    return s

def chunk_code_blocks(s: str) -> List[Tuple[str,str]]:
    """
    Split s into [('text', ...), ('code', ...)] preserving fences ``` or ~~~.
    """
    pattern = re.compile(
        r"(```.*?```|~~~.*?~~~)", re.DOTALL
    )
    parts: List[Tuple[str,str]] = []
    last = 0
    for m in pattern.finditer(s):
        if m.start() > last:
            parts.append(("text", s[last:m.start()]))
        parts.append(("code", m.group(0)))
        last = m.end()
    if last < len(s):
        parts.append(("text", s[last:]))
    return parts

def is_probably_identifier(key: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_\-\.]+", key or ""))

# ---------------------------
# Translators
# ---------------------------

class Translator:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.openai_key = os.getenv("OPENAI_API_KEY","").strip()
        self.deepl_key  = os.getenv("DEEPL_API_KEY","").strip()
        self.engine_order = cfg.get("engine_order", ["openai","deepl"])

    def translate(self, text: str, to_lang: str) -> str:
        if not text.strip():
            return text
        last_err = None
        for engine in self.engine_order:
            try:
                if engine == "openai" and self.openai_key:
                    return self._openai(text, to_lang)
                if engine == "deepl" and self.deepl_key:
                    return self._deepl(text, to_lang)
            except Exception as e:
                last_err = e
                time.sleep(1)
                continue
        # どれも使えない場合は明示的にエラー
        if last_err:
            raise last_err
        raise RuntimeError("No translation engine available. Set OPENAI_API_KEY or DEEPL_API_KEY as Actions Secret.")

    # --- OpenAI Chat ---
    def _openai(self, text: str, to_lang: str) -> str:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.openai_key}", "Content-Type":"application/json"}
        # 低コスト・高品質モデル（必要なら任意で変更OK）
        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role":"system","content": f"Translate into {to_lang}. Preserve code, JSON keys, and formatting. Output text only."},
                {"role":"user","content": text}
            ],
            "temperature": 0.2
        }
        r = requests.post(url, headers=headers, json=payload, timeout=120)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    # --- DeepL ---
    def _deepl(self, text: str, to_lang: str) -> str:
        # to_lang は "JA" を期待
        t = to_lang.upper()
        if t.startswith("JA"): t = "JA"
        url = "https://api-free.deepl.com/v2/translate"
        if self.deepl_key.startswith("dp_"):
            url = "https://api.deepl.com/v2/translate"  # Proキー
        data = {"text": text, "target_lang": t}
        headers = {"Authorization": f"DeepL-Auth-Key {self.deepl_key}"}
        r = requests.post(url, data=data, headers=headers, timeout=120)
        r.raise_for_status()
        return r.json()["translations"][0]["text"]

# ---------------------------
# Core: translate files
# ---------------------------

def translate_plain_file(p: Path, tr: Translator, to_lang: str, glossary: list) -> None:
    s = read_text(p)
    out_parts: List[str] = []
    for kind, chunk in chunk_code_blocks(s):
        if kind == "code":
            out_parts.append(chunk)        # そのまま
        else:
            jp = tr.translate(chunk, to_lang)
            jp = apply_glossary(jp, glossary)
            out_parts.append(jp)
    write_text(p, "".join(out_parts))

def translate_json_value(v: Any, key: str, tr: Translator, to_lang: str, cfg_json: dict, glossary: list) -> Any:
    if isinstance(v, str):
        # 値の翻訳可否判定
        mode = cfg_json.get("mode", "include_except_ignored")
        ignore_keys = set(cfg_json.get("ignore_keys", []))
        prefer_keys = set(cfg_json.get("prefer_keys", []))
        include_keys = set(cfg_json.get("include_keys", []))

        do_translate = False
        if key in prefer_keys:
            do_translate = True
        elif key in ignore_keys:
            do_translate = False
        elif mode == "include_except_ignored":
            # 識別子っぽい短文は避ける
            if key and not (key in ignore_keys or is_probably_identifier(v) and len(v) <= 32):
                do_translate = True
        elif mode == "include_only":
            if key in include_keys:
                do_translate = True

        if do_translate:
            # JSONの値でも、コードブロックが含まれるなら分割翻訳
            parts = chunk_code_blocks(v)
            out = []
            for kind, c in parts:
                if kind == "code":
                    out.append(c)
                else:
                    jp = tr.translate(c, to_lang)
                    jp = apply_glossary(jp, glossary)
                    out.append(jp)
            return "".join(out)
        return v

    if isinstance(v, list):
        return [translate_json_value(x, key, tr, to_lang, cfg_json, glossary) for x in v]
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            out[k] = translate_json_value(x, k, tr, to_lang, cfg_json, glossary)
        return out
    return v

def translate_json_file(p: Path, tr: Translator, to_lang: str, cfg_json: dict, glossary: list) -> None:
    obj = json.loads(read_text(p))
    obj2 = translate_json_value(obj, "", tr, to_lang, cfg_json, glossary)
    write_text(p, json.dumps(obj2, ensure_ascii=False, indent=2) + "\n")

# ---------------------------
# Main
# ---------------------------

def main() -> None:
    cfg = load_yaml(CFG_PATH)
    glossary = load_glossary(GLOSSARY_PATH)
    tr = Translator(cfg)
    target_lang = cfg.get("target_lang", "ja")

    # 1) TEXT: .txt / .md
    if cfg.get("translate_text", {}).get("enabled", True):
        exts = set(cfg.get("translate_text", {}).get("exts", [".txt",".md"]))
        excludes = [re.compile(p) for p in cfg.get("translate_text", {}).get("exclude", [])]
        for p in ROOT.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() in exts:
                rel = str(p.relative_to(ROOT)).replace("\\","/")
                if any(rx.search(rel) for rx in excludes):
                    continue
                print(f"[text] {rel}")
                translate_plain_file(p, tr, target_lang, glossary)

    # 2) JSON
    if cfg.get("translate_json", {}).get("enabled", True):
        cfg_json = cfg["translate_json"]
        for p in ROOT.rglob("*.json"):
            rel = str(p.relative_to(ROOT)).replace("\\","/")
            # 翻訳キット自身のJSONは除外
            if rel.startswith(".github/translation/"):
                continue
            print(f"[json] {rel}")
            translate_json_file(p, tr, target_lang, cfg_json, glossary)

if __name__ == "__main__":
    main()

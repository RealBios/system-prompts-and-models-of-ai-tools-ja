# .github/translation/translate_repo.py
from __future__ import annotations
import os, re, json, time, csv, sys, hashlib
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml
import requests

ROOT = Path(".").resolve()
CFG_PATH = ROOT / ".github" / "translation" / "translate.config.yml"
GLOSSARY_PATH = ROOT / ".github" / "translation" / "glossary_ja_en.csv"

# 翻訳対象フォルダを限定
TARGET_FOLDERS = {
    "Claude Code",
    "Cursor Prompts", 
    "Kiro",
    "Manus Agent Tools & Prompt",
    "VSCode Agent",
    "Windsurf"
}

# =========================
# Helpers
# =========================

def load_yaml(p: Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8"))

def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")

def write_text(p: Path, s: str) -> None:
    p.write_text(s, encoding="utf-8")

def load_glossary(p: Path) -> List[Tuple[str, str]]:
    if not p.exists():
        return []
    rows: List[Tuple[str, str]] = []
    with p.open("r", encoding="utf-8", newline="") as f:
        for row in csv.reader(f):
            if not row: 
                continue
            src = (row[0] or "").strip()
            dst = (row[1] or "").strip() if len(row) > 1 else ""
            if src and dst:
                rows.append((src, dst))
    # 長い語句から先に置換（より確実な置換のため）
    rows.sort(key=lambda x: len(x[0]), reverse=True)
    print(f"Loaded {len(rows)} glossary terms")
    return rows

def apply_glossary(s: str, glossary: List[Tuple[str, str]]) -> str:
    for src, dst in glossary:
        # 完全一致と大文字小文字を区別しない置換の両方を行う
        s = s.replace(src, dst)
        # 単語境界での置換（より確実）
        import re
        pattern = re.compile(r'\b' + re.escape(src) + r'\b', re.IGNORECASE)
        s = pattern.sub(dst, s)
    return s

def chunk_code_blocks(s: str) -> List[Tuple[str, str]]:
    """
    Split into [('text', ...), ('code', ...)] preserving fenced code blocks.
    Fences: ``` ``` and ~~~ ~~~ (non-greedy).
    """
    pattern = re.compile(r"(```.*?```|~~~.*?~~~)", re.DOTALL)
    parts: List[Tuple[str, str]] = []
    last = 0
    for m in pattern.finditer(s):
        if m.start() > last:
            parts.append(("text", s[last:m.start()]))
        parts.append(("code", m.group(0)))
        last = m.end()
    if last < len(s):
        parts.append(("text", s[last:]))
    return parts

def split_for_api(text: str, max_chars: int = 4000) -> List[str]:
    """段落単位でできるだけ自然に分割（各APIのトークン制限対策）"""
    if len(text) <= max_chars:
        return [text]
    parts: List[str] = []
    buf: List[str] = []
    size = 0
    for para in text.split("\n\n"):
        chunk = para + "\n\n"
        if size + len(chunk) > max_chars and buf:
            parts.append("".join(buf))
            buf = [chunk]
            size = len(chunk)
        else:
            buf.append(chunk)
            size += len(chunk)
    if buf:
        parts.append("".join(buf))
    return parts

def estimate_translation_time(text: str, file_delay: int = 1) -> int:
    """翻訳時間を推定（秒）"""
    chunks = split_for_api(text, 4000)
    # 各チャンクに対してAPI呼び出し + ファイル間待機
    api_calls = len(chunks)
    estimated_time = api_calls * 2 + file_delay  # API呼び出し約2秒/回 + 待機時間
    return estimated_time

def is_probably_identifier(s: str) -> bool:
    # 短い識別子やパス/IDっぽいものは翻訳しない方が安全
    return bool(re.fullmatch(r"[A-Za-z0-9_\-\.\/:]+", s or ""))

def get_file_batch(p: Path, batch_total: int) -> int:
    """ファイルパスからバッチ番号を決定（ハッシュベース）"""
    if batch_total <= 1:
        return 0
    path_hash = hashlib.md5(str(p).encode()).hexdigest()
    return int(path_hash, 16) % batch_total

def should_process_file(p: Path, batch_current: int, batch_total: int) -> bool:
    """このバッチで処理すべきファイルか判定"""
    return get_file_batch(p, batch_total) == batch_current

def is_in_target_folder(p: Path) -> bool:
    """ファイルが翻訳対象フォルダ内にあるかチェック"""
    try:
        rel_path = p.relative_to(ROOT)
        # パスの最初の部分（フォルダ名）をチェック
        first_part = str(rel_path).split(os.sep)[0]
        return first_part in TARGET_FOLDERS
    except ValueError:
        # ROOTの外のファイルは対象外
        return False

# =========================
# Translators
# =========================

class Translator:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.openai_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.deepl_key  = os.getenv("DEEPL_API_KEY", "").strip()
        print(f"Debug: OpenAI key present: {bool(self.openai_key)}")
        print(f"Debug: DeepL key present: {bool(self.deepl_key)}")
        self.engine_order = cfg.get("engine_order", ["openai", "deepl"])
        # 環境変数 ENGINE_ORDER で上書き可（例: "openai" or "deepl,openai"）
        env_order = os.getenv("ENGINE_ORDER", "").strip()
        if env_order:
            self.engine_order = [x.strip() for x in env_order.split(",") if x.strip()]
        print(f"Debug: Engine order: {self.engine_order}")

    def translate(self, text: str, to_lang: str) -> str:
        if not text.strip():
            return text

        chunks = split_for_api(text, max_chars=4000)
        out: List[str] = []
        last_err: Exception | None = None

        for i, ch in enumerate(chunks):
            translated: str | None = None
            for engine in self.engine_order:
                try:
                    if engine == "openai" and self.openai_key:
                        translated = self._openai(ch, to_lang)
                        break
                    if engine == "deepl" and self.deepl_key:
                        translated = self._deepl(ch, to_lang)
                        break
                except Exception as e:
                    last_err = e
                    print(f"Translation failed for chunk {i+1}/{len(chunks)} with {engine}: {e}")
                    # 次のエンジンへフォールバック
                    continue
            
            if translated is None:
                # 翻訳に失敗した場合、元のテキストを返す（部分翻訳を避ける）
                print(f"All translation engines failed for chunk {i+1}/{len(chunks)}. Using original text.")
                return text  # 元のテキストを返す
            
            out.append(translated)
            
            # チャンク間の短い待機（レート制限対策）
            if i < len(chunks) - 1:
                time.sleep(0.5)

        return "".join(out)

    # --- OpenAI Chat completions ---
    def _openai(self, text: str, to_lang: str) -> str:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.openai_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"Translate the following content completely into {to_lang}. "
                        "IMPORTANT: Translate ALL text content to Japanese. Do not leave any English words untranslated. "
                        "Preserve code blocks, JSON keys, placeholders, and formatting exactly as they are. "
                        "Use consistent Japanese terminology throughout. "
                        "Do not add extra commentary or explanations."
                    ),
                },
                {"role": "user", "content": text},
            ],
            "temperature": 0.1,  # より一貫した翻訳のため温度を下げる
        }
        # 最小限のリトライ（429/5xx）- 極めて短縮された待機時間
        for attempt in range(2):  # 5回から2回に削減
            r = requests.post(url, headers=headers, json=payload, timeout=60)  # タイムアウトを60秒に短縮
            if r.status_code in (429, 500, 502, 503):
                # 極めて短縮された待機: 5, 10秒
                wait_time = 5 * (attempt + 1)
                print(f"Rate limited (attempt {attempt+1}/2), waiting {wait_time} seconds...")
                time.sleep(wait_time)
                continue
            r.raise_for_status()
            j = r.json()
            return j["choices"][0]["message"]["content"]
        r.raise_for_status()
        return ""  # unreachable

    # --- DeepL ---
    def _deepl(self, text: str, to_lang: str) -> str:
        t = to_lang.upper()
        if t.startswith("JA"):
            t = "JA"
        url = "https://api-free.deepl.com/v2/translate"
        if self.deepl_key.startswith("dp_"):
            url = "https://api.deepl.com/v2/translate"  # Proキー推定
        data = {"text": text, "target_lang": t}
        headers = {"Authorization": f"DeepL-Auth-Key {self.deepl_key}"}

        for attempt in range(2):  # 3回から2回に削減
            r = requests.post(url, data=data, headers=headers, timeout=60)  # タイムアウトを60秒に短縮
            if r.status_code == 456:
                # クォータ切れ/契約外 → 即フォールバック
                raise RuntimeError("DeepL 456 Unrecoverable (quota/plan).")
            if r.status_code in (429, 500, 502, 503):
                wait_time = 2 * (attempt + 1)  # 極めて短縮された待機: 2, 4秒
                print(f"DeepL rate limited (attempt {attempt+1}/2), waiting {wait_time} seconds...")
                time.sleep(wait_time)
                continue
            r.raise_for_status()
            j = r.json()
            return j["translations"][0]["text"]
        r.raise_for_status()
        return ""  # unreachable

# =========================
# File translators
# =========================

def translate_plain_file(p: Path, tr: Translator, to_lang: str, glossary: List[Tuple[str,str]]) -> None:
    s = read_text(p)
    out_parts: List[str] = []
    for kind, chunk in chunk_code_blocks(s):
        if kind == "code":
            out_parts.append(chunk)
        else:
            jp = tr.translate(chunk, to_lang)
            jp = apply_glossary(jp, glossary)
            out_parts.append(jp)
    write_text(p, "".join(out_parts))

def translate_json_value(
    v: Any,
    key: str,
    tr: Translator,
    to_lang: str,
    cfg_json: dict,
    glossary: List[Tuple[str,str]],
) -> Any:
    if isinstance(v, str):
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
            # 識別子/短文/パスっぽい値は避ける
            if not is_probably_identifier(v) or len(v) > 32:
                do_translate = True
        elif mode == "include_only":
            if key in include_keys:
                do_translate = True

        if do_translate:
            parts = chunk_code_blocks(v)
            out: List[str] = []
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
        out: Dict[str, Any] = {}
        for k, x in v.items():
            out[k] = translate_json_value(x, k, tr, to_lang, cfg_json, glossary)
        return out
    return v

def translate_json_file(p: Path, tr: Translator, to_lang: str, cfg_json: dict, glossary: List[Tuple[str,str]]) -> None:
    # 厳密JSONで読み、構造は保ったまま出力
    obj = json.loads(read_text(p))
    obj2 = translate_json_value(obj, "", tr, to_lang, cfg_json, glossary)
    write_text(p, json.dumps(obj2, ensure_ascii=False, indent=2) + "\n")

# =========================
# Main
# =========================

def main() -> None:
    if not CFG_PATH.exists():
        raise FileNotFoundError(f"Missing config: {CFG_PATH}")

    cfg = load_yaml(CFG_PATH)
    glossary = load_glossary(GLOSSARY_PATH)

    target_lang = cfg.get("target_lang", "ja")
    tr = Translator(cfg)

    # 環境変数でファイル間待機時間を制御（デフォルト3秒に短縮）
    file_delay = int(os.getenv("TRANSLATE_FILE_DELAY", "3"))
    print(f"Debug: File processing delay: {file_delay} seconds")

    # バッチ処理設定（GitHub Actionsでの並列実行用）
    batch_current = int(os.getenv("BATCH_CURRENT", "0"))
    batch_total = int(os.getenv("BATCH_TOTAL", "1"))
    if batch_total > 1:
        print(f"Debug: Running batch {batch_current + 1}/{batch_total}")
    else:
        print("Debug: Running single batch mode")

    # --- TEXT (.txt, .md)
    text_cfg = cfg.get("translate_text", {})
    if text_cfg.get("enabled", True):
        exts = set(text_cfg.get("exts", [".txt", ".md"]))
        excludes = [re.compile(p) for p in text_cfg.get("exclude", [])]
        
        # 対象ファイルを事前に収集
        target_files = []
        for p in ROOT.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() in exts:
                if not is_in_target_folder(p):
                    continue
                rel = str(p.relative_to(ROOT)).replace("\\", "/")
                if any(rx.search(rel) for rx in excludes):
                    continue
                if not should_process_file(p, batch_current, batch_total):
                    continue
                target_files.append(p)
        
        print(f"Processing {len(target_files)} text files...")
        for i, p in enumerate(target_files, 1):
            rel = str(p.relative_to(ROOT)).replace("\\", "/")
            print(f"[{i}/{len(target_files)}] [text] {rel}")
            try:
                translate_plain_file(p, tr, target_lang, glossary)
                print(f"✓ Completed: {rel}")
            except Exception as e:
                print(f"✗ Failed: {rel} - {e}")
                # 失敗しても処理を継続
                continue
            
            # ファイル間レート制限対策（無効化されている場合はスキップ）
            if file_delay > 0:
                time.sleep(file_delay)

    # --- JSON (*.json)
    json_cfg = cfg.get("translate_json", {})
    if json_cfg.get("enabled", True):
        # 対象JSONファイルを事前に収集
        json_files = []
        for p in ROOT.rglob("*.json"):
            rel = str(p.relative_to(ROOT)).replace("\\", "/")
            if rel.startswith(".github/translation/"):
                continue  # 翻訳ツール自身は除外
            if not is_in_target_folder(p):
                continue
            if not should_process_file(p, batch_current, batch_total):
                continue
            json_files.append(p)
        
        print(f"Processing {len(json_files)} JSON files...")
        for i, p in enumerate(json_files, 1):
            rel = str(p.relative_to(ROOT)).replace("\\", "/")
            print(f"[{i}/{len(json_files)}] [json] {rel}")
            try:
                translate_json_file(p, tr, target_lang, json_cfg, glossary)
                print(f"✓ Completed: {rel}")
            except Exception as e:
                print(f"✗ Failed: {rel} - {e}")
                # 失敗しても処理を継続
                continue
            
            # ファイル間レート制限対策（無効化されている場合はスキップ）
            if file_delay > 0:
                time.sleep(file_delay)
    
    print("All translation tasks completed!")

if __name__ == "__main__":
    main()

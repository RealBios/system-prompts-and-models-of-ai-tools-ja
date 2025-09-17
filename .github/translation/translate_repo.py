#!/usr/bin/env python3
import os, sys, re, json, argparse, csv
from pathlib import Path

USE_OPENAI = bool(os.environ.get("OPENAI_API_KEY"))
USE_DEEPL = bool(os.environ.get("DEEPL_API_KEY"))

def load_config(p: Path):
    import yaml
    return yaml.safe_load(p.read_text(encoding="utf-8"))

def load_glossary(p: Path):
    d = {}
    if not p.exists(): return d
    for row in csv.DictReader(p.open(encoding="utf-8")):
        en = row.get("en","").strip()
        ja = row.get("ja","").strip()
        if en and ja: d[en] = ja
    return d

def split_md_blocks(text: str):
    parts = re.split(r"(```.*?```)", text, flags=re.DOTALL)
    for i, part in enumerate(parts):
        if i % 2 == 1:
            yield ("code", part)
        else:
            yield ("text", part)

def rule_based_translate(s: str, glossary: dict):
    reps = [
        (r'\bExecutes?\b', '実行します'),
        (r'\bSearch(?:es)?\b', '検索します'),
        (r'\bCreate(?:s)?\b', '作成します'),
        (r'\bOpen(?:s)?\b', '開きます'),
        (r'\bRead(?:s)?\b', '読み取ります'),
        (r'\bWrite(?:s)?\b', '書き込みます'),
        (r'\bUpdate(?:s)?\b', '更新します'),
        (r'\bDelete(?:s)?\b', '削除します'),
        (r'\bList(?:s)?\b', '一覧を取得します'),
        (r'\bReturn(?:s)?\b', '返します'),
        (r'\bCheck(?:s)?\b', '確認します'),
        (r'\bValidate(?:s)?\b', '検証します'),
        (r'\bGenerate(?:s)?\b', '生成します'),
        (r'\bUpload(?:s)?\b', 'アップロードします'),
        (r'\bDownload(?:s)?\b', 'ダウンロードします'),
        (r'\bConvert(?:s)?\b', '変換します'),
        (r'\bCompare(?:s)?\b', '比較します'),
        (r'\bExtract(?:s)?\b', '抽出します'),
        (r'\bFilter(?:s)?\b', 'フィルタします'),
        (r'\bSort(?:s)?\b', 'ソートします'),
        (r'\bSummarize(?:s)?\b', '要約します'),
        (r'\bTranslate(?:s)?\b', '翻訳します'),
        (r'\bAnalyze(?:s)?\b', '分析します'),
        (r'\bSchedule(?:s)?\b', 'スケジュールします'),
        (r'\bNotify(?:ies)?\b', '通知します'),
        (r'\bfile(s)?\b', 'ファイル'),
        (r'\bfolder(s)?\b', 'フォルダ'),
        (r'\bdirectory(ies)?\b', 'ディレクトリ'),
        (r'\bpath(s)?\b', 'パス'),
        (r'\bcommand(s)?\b', 'コマンド'),
        (r'\bscript(s)?\b', 'スクリプト'),
        (r'\brepository\b', 'リポジトリ'),
        (r'\bbranch(es)?\b', 'ブランチ'),
        (r'\bcommit(s)?\b', 'コミット'),
        (r'\bpull request(s)?\b', 'プルリクエスト'),
        (r'\bissue(s)?\b', 'Issue'),
        (r'\btoken(s)?\b', 'トークン'),
        (r'\bparameter(s)?\b', 'パラメータ'),
        (r'\bproperty(ies)?\b', 'プロパティ'),
        (r'\bschema\b', 'スキーマ'),
        (r'\bmodel(s)?\b', 'モデル'),
        (r'\btool(s)?\b', 'ツール'),
        (r'\bquery\b', 'クエリ'),
        (r'\blog(s)?\b', 'ログ'),
        (r'\berror(s)?\b', 'エラー'),
        (r'\bwarning(s)?\b', '警告'),
        (r'\boutput\b', '出力'),
        (r'\binput\b', '入力'),
        (r'\bresult(s)?\b', '結果'),
        (r'\bexample(s)?\b', '例'),
        (r'\bdefault\b', '既定値'),
    ]
    out = s
    for pat, rep in reps:
        out = re.sub(pat, rep, out, flags=re.IGNORECASE)
    for en, ja in glossary.items():
        out = out.replace(en, ja)
    out = out.replace(" e.g.", " 例:").replace(" i.e.", " すなわち ")
    return out

def openai_translate(text: str):
    from openai import OpenAI
    client = OpenAI()
    resp = client.chat.completions.create(
        model=os.environ.get("OPENAI_MODEL","gpt-5"),
        temperature=0.2,
        messages=[
            {"role":"system","content":"Translate to Japanese precisely. Preserve code fences and JSON keys."},
            {"role":"user","content":text}
        ]
    )
    return resp.choices[0].message.content.strip()

def deepl_translate(text: str):
    import deepl
    translator = deepl.Translator(os.environ["DEEPL_API_KEY"])
    return translator.translate_text(text, target_lang="JA").text

def translate_text_block(text: str, glossary: dict, bilingual=False):
    if not text.strip():
        return text
    try:
        if USE_OPENAI:
            ja = openai_translate(text)
        elif USE_DEEPL:
            ja = deepl_translate(text)
        else:
            ja = rule_based_translate(text, glossary)
    except Exception:
        ja = rule_based_translate(text, glossary)
    return ja if not bilingual else f"{ja}\n\n[English]\n{text}"

def translate_markdown_like(text: str, glossary: dict, bilingual=False):
    out = []
    for kind, part in split_md_blocks(text):
        if kind == "code":
            out.append(part)
        else:
            out.append(translate_text_block(part, glossary, bilingual=bilingual))
    return "".join(out)

def translate_json_descriptions(obj, glossary: dict, bilingual=False):
    if isinstance(obj, dict):
        new = {}
        for k, v in obj.items():
            if k == "description" and isinstance(v, str):
                new[k] = translate_text_block(v, glossary, bilingual=bilingual)
            else:
                new[k] = translate_json_descriptions(v, glossary, bilingual=bilingual)
        return new
    elif isinstance(obj, list):
        return [translate_json_descriptions(x, glossary, bilingual=bilingual) for x in obj]
    else:
        return obj

def main():
    import yaml
    cfg = yaml.safe_load(Path(".github/translation/translate.config.yml").read_text(encoding="utf-8"))
    bilingual = bool(cfg.get("bilingual", False))
    glossary = {}
    gl = cfg.get("glossary_csv")
    if gl:
        glossary = load_glossary(Path(gl))

    # JSON
    if cfg.get("translate_json",{}).get("enabled", True):
        for p in Path(".").rglob("*.json"):
            if ".github/translation/" in str(p): 
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            new_data = translate_json_descriptions(data, glossary, bilingual=bilingual)
            p.write_text(json.dumps(new_data, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")

    # Text
    tx = cfg.get("translate_text",{})
    if tx.get("enabled", True):
        exts = set(tx.get("exts", [".txt",".md"]))
        excludes = [re.compile(r) for r in tx.get("exclude", [])]
        for p in Path(".").rglob("*"):
            if not p.is_file(): continue
            if p.suffix.lower() not in exts: continue
            sp = str(p)
            if any(rx.search(sp) for rx in excludes): continue
            try:
                raw = p.read_text(encoding="utf-8")
            except Exception:
                continue
            new = translate_markdown_like(raw, glossary, bilingual=bilingual)
            p.write_text(new, encoding="utf-8")

if __name__ == "__main__":
    main()

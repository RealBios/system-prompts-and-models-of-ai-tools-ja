import json, re, pathlib
p = pathlib.Path(r"""Traycer AI\phase_mode_tools.json""")
s = p.read_text(encoding="utf-8", errors="replace")

# 1) 既に厳密JSONなら何もしない
try:
    json.loads(s)
    print("already valid"); raise SystemExit
except json.JSONDecodeError:
    pass

# 2) "description": " …… " の値の中だけで、未エスケープの " を \" に
def fix_desc(m):
    head, body = m.group(1), m.group(2)
    # 既に \" のものは除外して、それ以外の " をエスケープ
    body2 = re.sub(r'(?<!\\)"', r'\\"', body)
    return head + body2 + '"'

s2 = re.sub(r'("description"\s*:\s*")([\s\S]*?)"', fix_desc, s, count=1)

# 3) 厳密JSONで再整形（エラーなら例外→どこが壊れてるか確認できる）
obj = json.loads(s2)
p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("fixed")

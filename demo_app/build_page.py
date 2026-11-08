"""page_tpl.html 의 __CASES__ 자리에 data.json 을 넣어 static/index.html 을 만든다."""
from pathlib import Path

b = Path(__file__).resolve().parent
tpl = (b / "page_tpl.html").read_text(encoding="utf-8")
data = (b / "data.json").read_text(encoding="utf-8").strip().replace("</", "<\\/")
assert "__CASES__" in tpl
(b / "static").mkdir(exist_ok=True)
# 게시 페이지는 조각(fragment)이므로 로컬 서빙용 문서 머리만 앞에 붙인다.
head = ('<!doctype html>\n<html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n')
(b / "static" / "index.html").write_text(head + tpl.replace("__CASES__", data), encoding="utf-8", newline="\n")
print("static/index.html 생성")

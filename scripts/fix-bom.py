# -*- coding: utf-8 -*-
"""给 .ps1 / .bat 补回被编辑工具剥掉的 UTF-8 BOM。

为什么需要它：项目约定 .ps1 必须带 BOM，否则 Windows PowerShell 5.1 会把
中文注释按 GBK 解码，轻则注释乱码，重则整份脚本解析失败。

踩坑记录：用 edit 工具改 .ps1 时 BOM 会被悄悄剥掉，表现为「脚本突然报一堆
语法错」而不是「编码不对」，很难第一时间联想到。改完 .ps1 务必跑本脚本。
"""
import pathlib
import sys

fixed, missing = [], []

for arg in sys.argv[1:]:
    p = pathlib.Path(arg)
    if not p.is_file():
        print("SKIP   %s (not a file)" % p)
        continue
    raw = p.read_bytes()
    has_bom = raw[:3] == b"\xef\xbb\xbf"
    if has_bom:
        missing.append(str(p))          # 已经是 BOM，记为"无需处理"
        continue
    try:
        raw.decode("utf-8")              # 先确认是合法 UTF-8，否则不加 BOM（可能本来就是 GBK 的 .bat）
    except UnicodeDecodeError as e:
        print("SKIP   %s (非 UTF-8，跳过：%s)" % (p.name, e))
        continue
    p.write_bytes(b"\xef\xbb\xbf" + raw)
    fixed.append(str(p))
    print("FIXED  %s  (+BOM, %d bytes)" % (p.name, len(raw) + 3))

print("补 BOM %d 个，已带 BOM %d 个" % (len(fixed), len(missing)))
sys.exit(0)
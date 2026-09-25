# -*- coding: utf-8 -*-
"""重新生成离线图标字体 fonts/material-symbols-rounded.woff2

前端是纯离线运行的，不能依赖 fonts.googleapis.com，所以图标字体被裁切后随程序一起分发。
裁切只保留 material3-upload.html 里真正用到的图标，体积约 5 KB。

什么时候需要重新跑：
    页面里新增/修改了图标名之后（HTML 里的 <span class="material-symbols-rounded">、
    JS 里 iconOf()/showToast()/textContent 等任何地方写到的图标名）。

用法（需要联网）：
    python tools/build_icon_font.py            # 重新生成并校验
    python tools/build_icon_font.py --dry-run  # 只列出识别到的图标，不写文件

依赖：pip install fonttools uharfbuzz

识别方式：先下载官方字体、取出全部合法图标名，再在页面里找出现的名字。
这样无论图标名写在 HTML 还是 JS 里都能识别，也不怕以后写法变化。
"""
import argparse
import io
import os
import re
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML_FILE = os.path.join(ROOT, 'web', 'index.html')
OUT_FILE = os.path.join(ROOT, 'web', 'fonts', 'material-symbols-rounded.woff2')

# 与前端 CSS 里 font-variation-settings 对应的轴取值（静态实例，体积最小）
AXES = 'opsz,wght,FILL,GRAD@24,400,0,0'
CSS_API = 'https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:' + AXES
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

# 页面里可能写成图标名的位置：引号字符串、HTML 元素文本
TOKEN_RE = re.compile(r"""['"]([a-z0-9_]{2,40})['"]|>\s*([a-z0-9_]{2,40})\s*<""")


def fetch(url, binary=False):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    return data if binary else data.decode('utf-8')


def fetch_source_font():
    """让 Google 返回静态实例，再从 CSS 里取出字体地址"""
    css = fetch(CSS_API)
    m = re.search(r'url\((https://[^)]+)\)', css)
    if not m:
        raise RuntimeError('未能从 Google Fonts CSS 中解析出字体地址')
    return fetch(m.group(1), binary=True)


def main():
    ap = argparse.ArgumentParser(description='重新生成离线图标字体子集')
    ap.add_argument('--dry-run', action='store_true', help='只列出识别到的图标，不写文件')
    args = ap.parse_args()

    try:
        import uharfbuzz as hb
        from fontTools.ttLib import TTFont
        from fontTools import subset
    except ImportError as e:
        print(f'缺少依赖: {e}\n请先执行: pip install fonttools uharfbuzz', file=sys.stderr)
        return 1

    print('下载官方图标字体…')
    src = TTFont(io.BytesIO(fetch_source_font()))
    src.flavor = None
    buf = io.BytesIO()
    src.save(buf)
    ttf_bytes = buf.getvalue()

    order = src.getGlyphOrder()
    cmap = src.getBestCmap()
    src_gid_to_cp = {order.index(g): cp for cp, g in cmap.items()}
    font = hb.Font(hb.Face(hb.Blob(ttf_bytes)))

    def shape(s):
        """返回排版后的 glyph 名列表"""
        b = hb.Buffer()
        b.add_str(s)
        b.guess_segment_properties()
        hb.shape(font, b, {'liga': True, 'clig': True})
        return [order[i.codepoint] for i in b.glyph_infos]

    # 页面里出现的候选词，能排成单个 glyph 的就是合法图标名
    candidates = set()
    html = open(HTML_FILE, encoding='utf-8').read()
    for a, b in TOKEN_RE.findall(html):
        candidates.add(a or b)

    names = []
    for t in sorted(candidates):
        got = shape(t)
        if len(got) == 1 and got[0] != '.notdef':
            names.append(t)

    print(f'\n从 {os.path.basename(HTML_FILE)} 识别到 {len(names)} 个图标:')
    for i in range(0, len(names), 6):
        print('   ' + '  '.join(names[i:i + 6]))

    # 反向检查：span 里写了但字体里没有的名字（会渲染成文字）
    used_in_spans = set(re.findall(r'material-symbols-rounded"[^>]*>\s*([a-z0-9_]+)\s*<', html))
    missing = sorted(n for n in used_in_spans if n not in names)
    if missing:
        print('\n⚠️ 以下图标名在官方字体里不存在，页面上会显示成文字：')
        for n in missing:
            print('   -', n)

    if args.dry_run:
        return 1 if missing else 0

    # 1) 图标名的 ligature 链：逐前缀排版，收集中间 glyph
    # 2) 输入字符本身的 glyph：保证裁切后 cmap 仍保留这些字符，浏览器才能输入 ligature 序列
    needed, expected = set(), {}
    for name in names:
        got = shape(name)
        expected[name] = got[0]
        needed.update(got)
        for i in range(1, len(name) + 1):
            needed.update(shape(name[:i]))
    for ch in sorted(set(' '.join(names))):
        g = cmap.get(ord(ch))
        if g is None:
            raise RuntimeError(f'字符 {ch!r} 不在字体 cmap 中')
        needed.add(g)
    needed.discard('.notdef')

    print(f'\n裁切到 {len(needed)} 个 glyph…')
    opts = subset.Options()
    opts.flavor = 'woff2'
    opts.layout_features = ['*']
    opts.layout_closure = False          # 关闭闭包，否则会连带拉进上千个无关图标
    opts.hinting = False
    opts.desubroutinize = True
    subsetter = subset.Subsetter(options=opts)
    subsetter.populate(glyphs=needed)
    subsetter.subset(src)
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    src.flavor = 'woff2'
    src.save(OUT_FILE)

    # 校验：裁切后每个图标名都要排成同一个 glyph（按码位比对，glyph 名可能被重命名）
    out = TTFont(OUT_FILE)
    out.flavor = None
    obuf = io.BytesIO()
    out.save(obuf)
    out_order = out.getGlyphOrder()
    out_gid_to_cp = {out_order.index(g): cp for cp, g in out.getBestCmap().items()
                     if g in out_order}
    out_font = hb.Font(hb.Face(hb.Blob(obuf.getvalue())))

    def shape_with(f, idmap, s):
        b = hb.Buffer()
        b.add_str(s)
        b.guess_segment_properties()
        hb.shape(f, b, {'liga': True, 'clig': True})
        return [idmap.get(i.codepoint) for i in b.glyph_infos]

    bad = [(n, shape_with(font, src_gid_to_cp, n), shape_with(out_font, out_gid_to_cp, n))
           for n in expected]
    bad = [(n, w, g) for n, w, g in bad if w != g or len(g) != 1]

    size = os.path.getsize(OUT_FILE)
    print(f'已写入 {os.path.relpath(OUT_FILE, ROOT)} ({size} 字节)')
    if bad:
        print('\n❌ 校验失败，以下图标裁切后渲染结果与官方字体不一致：')
        for name, want, got in bad:
            print(f'   - {name}: 期望 {want}，实际 {got}')
        return 1
    print(f'✅ 校验通过：{len(expected)} 个图标全部渲染正确')
    return 1 if missing else 0


if __name__ == '__main__':
    sys.exit(main())

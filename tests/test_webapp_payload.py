r"""阶段 7.1 验收：网页版载荷层（webapp/payload.py）

自建前端的后端只做两件事：把引擎结果整理成 JSON（本文件）、把字节搬来搬去（server.py）。
这里守住载荷层的约定（都是"前端能不能正确渲染"的前提）：

  ① **图片只用 id 引用**，绝不内嵌 base64（demo 就是被 0.94MB 内嵌图片拖慢的）；
  ② 卡片段落类型齐全：heading / text 给文本，formula / table 给区域 id，figure 给插图 id；
  ③ 卡片自带的插图（该卡关联的论文图）要出现在这张卡的段落里（靠 associate_with_cards 写的
     card_order 关联——这条是端到端自测抓出来的真 bug，留在断言里防复发）；
  ④ 元数据十项都带 value/source/found/note（前端要如实显示来源与警告）；
  ⑤ 整份响应可 JSON 序列化、且不含服务端内部字段 `_registry`。

样本不在本机时用合成对象兜底，保证仓库在任何机器上都能跑。
"""

import json
import os
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

from webapp import payload as payload_module       # noqa: E402

SAMPLE = r"E:\segemented pdf reader\测试pdf\单栏.pdf"
passed, failed, skipped = 0, 0, 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [OK] {label}")
    else:
        failed += 1
        print(f"  [!!] {label}  {detail}")


def skip(label):
    global skipped
    skipped += 1
    print(f"  [跳过] {label}（样本不在本机）")


print("=" * 78)
print("① 图片 id 与区域 id 的生成规则")
print("-" * 78)
check("区域 id 由「页 + 坐标」决定，稳定可复用（刷新页面能命中浏览器缓存）",
      payload_module.region_id(4, [215.9, 512.5, 269.6, 526.2])
      == payload_module.region_id(4, [215.1, 512.9, 270.0, 526.0])
      == "r_4_215_512",
      payload_module.region_id(4, [215.9, 512.5, 269.6, 526.2]))
check("不同页 / 不同坐标的 id 不同",
      payload_module.region_id(4, [10, 10, 20, 20]) != payload_module.region_id(5, [10, 10, 20, 20]))


class FakeImage:
    """只用得到这几个属性的假图片对象（鸭子类型）"""
    def __init__(self, key, page=1, card_order=None):
        self.key = key
        self.page = page
        self.card_order = card_order
        self.pixel_w, self.pixel_h = 1200, 800
        self.thumb = b"\x89PNG-fake"
        self.xref, self.smask = 7, 0


check("插图 id 稳定且带图片 key（便于肉眼核对）",
      payload_module.figure_id(3, FakeImage("abcDEF123")) == "fig_3_abcDEF123")

print()
print("② 用合成对象跑一遍载荷组装（不依赖样本）")
print("-" * 78)


class FakeCard:
    def __init__(self, index, segments, order=0):
        self.card_index = index
        self.order = order
        self.page, self.page_end = 1, 2
        self.section_number, self.section_title = "1", "Introduction"
        self.segments = segments
        self.text = " ".join(s[1] for s in segments if isinstance(s[1], str))
        self.is_formula = self.is_table = False


class FakeMeta:
    def __init__(self):
        for name in ("title", "doi", "abstract", "authors", "first_author",
                     "corresponding_author", "corresponding_email", "affiliations",
                     "supplementary_links", "author_emails"):
            setattr(self, name, type("F", (), {"value": "x", "source": "字号分析",
                                               "found": True, "note": ""})())
        self.supplementary_items = []


fake_card = FakeCard(0, [("heading", "1 Introduction"), ("text", "Hello world."),
                         ("formula", {"page": 4, "rect": [10, 20, 60, 40], "text": "E=mc²"})])
fake_image = FakeImage("imgAAA", page=1, card_order=0)
data = payload_module.build_paper_payload(
    b"%PDF-1.7 fake", "x.pdf",
    {"cards": [fake_card], "pages": [{"页码": 1, "检测排版": "单栏"}], "elapsed": 1.2,
     "body_size": 10.0, "total_chars": 100, "formula_clusters": [1, 2], "table_regions": []},
    {"images": [fake_image]}, None, FakeMeta())

card = data["cards"][0]
kinds = [seg["kind"] for seg in card["segments"]]
check("段落类型齐全：heading / text / formula / figure",
      kinds == ["heading", "text", "formula", "figure"], repr(kinds))
check("公式段给的是区域 id + 说明（不是 base64 图片）",
      card["segments"][2]["id"].startswith("r_") and "原文第 4 页" in card["segments"][2]["meta"])
check("该卡关联的插图出现在这张卡的段落里（card_order 关联）",
      card["figures"] == [card["segments"][3]["id"]] and fake_image.card_order == 0)
# 注意：`_registry` 里装的是图片对象（不可 JSON 序列化），**必须由调用方 pop 掉再发**。
# server.py 与 state.open() 都是这么做的；这里把这一步显式跑一遍，防止有人直接 json.dumps(data)。
registry = data.pop("_registry")
body = json.dumps(data, ensure_ascii=False)
check("pop 掉 _registry 之后整份响应可 JSON 序列化（前端直接 JSON.parse）",
      isinstance(body, str) and '"cards"' in body)
check("响应里没有任何 base64 图片", "base64" not in body)
check("_registry 单独交出：装了图片对象，供 /api/img/<id> 查表",
      "r_4_10_20" in registry and registry["r_4_10_20"]["type"] == "region"
      and any(v["type"] == "figure" for v in registry.values()))
check("元数据十项 + 来源齐全",
      len([k for k in data["metadata"] if k != "supplementary_items"]) == 10
      and data["metadata"]["title"]["source"] == "字号分析")
check("概览数字来自解析结果（页数/卡片/词数/公式区域数）",
      data["overview"]["pages"] == 1 and data["overview"]["cards"] == 1
      and data["overview"]["words"] == payload_module.count_words(fake_card.text)
      and data["overview"]["formula_regions"] == 2,
      repr(data["overview"]))
check("缓存概况带目录与条数（右栏要显示给用户）",
      "root" in data["cache"] and "translations" in data["cache"])

print()
print("③ 真样本（有则跑，没有就跳过）")
print("-" * 78)
if not os.path.exists(SAMPLE):
    skip("真样本载荷组装")
else:
    from utils import image_extractor, metadata as metadata_module, pdf_parser
    pdf = open(SAMPLE, "rb").read()
    result = pdf_parser.parse_pdf(pdf, True, True, 200, "image")
    images = image_extractor.extract_images(pdf)
    image_extractor.associate_with_cards(images["images"], result["cards"])
    meta = metadata_module.extract_metadata(pdf, result["blocks"])
    real = payload_module.build_paper_payload(pdf, "单栏.pdf", result, images, None, meta)
    real.pop("_registry", None)
    check("真样本：卡片数与解析结果一致", len(real["cards"]) == len(result["cards"]))
    check("真样本：至少有一张卡带插图（说明图片关联生效）",
          any(c["figures"] for c in real["cards"]),
          str(sum(1 for c in real["cards"] if c["figures"])))
    check("真样本：公式区域以 id 引用并且能查到（>0 个）",
          sum(1 for c in real["cards"] for s in c["segments"] if s["kind"] == "formula") > 0)
    check("真样本：标题带着来源标注（前端要显示来源）",
          bool(real["metadata"]["title"]["source"]), real["metadata"]["title"]["source"][:40])

print()
print("④ 人工修正的元数据：叠加、标记、与 Streamlit 版共用同一套键")
print("-" * 78)


class FakeMeta2(FakeMeta):
    """带具体值的假元数据（用来验证"人工修正优先"）"""

    def __init__(self):
        super().__init__()
        self.title = type("F", (), {"value": "自动标题", "source": "字号分析",
                                    "found": True, "note": ""})()


view = payload_module.metadata_payload(FakeMeta2(), {"in_title": "人工改过的标题"})
check("人工修正的值优先显示", view["title"]["value"] == "人工改过的标题", view["title"]["value"])
check("自动提取的原值仍带着（供「还原」用）", view["title"]["auto"] == "自动标题")
check("被改过的字段有 edited 标记（界面显示 ✎）", view["title"]["edited"] is True)
check("没改过的字段 edited=False", view["doi"]["edited"] is False)
check("每个字段都带 store_key（前端提交时用它）",
      all(field.get("store_key", "").startswith("in_") for key, field in view.items()
          if key != "supplementary_items"))

# 关键兼容性：网页版与 Streamlit 版共用同一份 .cache/，人工修正的键必须完全一致，
# 否则一边改的元数据另一边看不见（这是"两套前端共用缓存"承诺的技术前提）
try:
    from ui import persist as ui_persist
    check("人工修正键与 Streamlit 版完全一致（两套前端共用一份记录）",
          set(payload_module.EDIT_KEYS.values()) == set(ui_persist.METADATA_WIDGET_KEYS),
          repr(set(payload_module.EDIT_KEYS.values()) ^ set(ui_persist.METADATA_WIDGET_KEYS)))
except Exception as exc:                                     # pragma: no cover
    check("能导入 ui.persist 做键一致性核对", False, repr(exc))

print()
print("=" * 78)
print(f"断言结果：通过 {passed} · 失败 {failed} · 跳过 {skipped}")
sys.exit(1 if failed else 0)

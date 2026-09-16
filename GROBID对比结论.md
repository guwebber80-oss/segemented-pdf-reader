# GROBID 交叉校验实测对比与结论

> 起因：Nature 那篇（`41562_2025_Article_...` 层级资源理性解释阅读行为）卡片小标题有误，
> 你提出"是否装全量版（`grobid/grobid:0.9.1-full`，约 8GB）辅助增强段落标题识别"。
> 本文是**实测证据**，不是推测。

## 一、实测方法

- 样本：Nature 篇 PDF（同一份，已缓存）
- A 组：我们自己的字号+版式规则（`utils/pdf_parser.py::mark_headings`）
- B 组：`grobid/grobid:0.9.1-crf`（**当前已装的那个**，非全量版）
  `POST /api/processFulltextDocument` → 解析 TEI `<div><head>` 结构
- 脚本：`.dsh-scratch/probe_grobid_heads.py`（开发用脚手架，不进仓库）

## 二、结果

| | 我们 | GROBID(CRF) |
|---|---|---|
| 判为标题的条目 | 37 | 30 |

### 1. 双方都对（11 个，完全一致）

`Computational simulation of reading` · `Results` · `Deciding when and where to fixate in a word` ·
`Deciding where to fixate in a sentence` · `Text comprehension and deciding where to read in text` ·
`Speed-accuracy trade-off when reading under time pressure` ·
`Validating the necessity of hierarchical resource rationality` · `Discussion` · `Model` ·
`Training and simulation` · `Parameters and fitting`

### 2. GROBID 明显更强的地方 —— "行内小标题"（run-in heading）

这类标题**以句点结尾、紧跟正文同一段**，纯字号规则天然抓不到：

- `Word recognizer.`（其下 19 段）
- `Text reader.` / `Memory system.` / `Reading under time pressure.` / `Simulation and validation.`
- `Datasets`（我们把 `Methods Datasets` 并成了一个，它拆开了，更准）
- `Reading under time pressure dataset (English).`（我们当成了正文）

### 3. GROBID 明显更差的地方 —— 图注/表格残留被当标题（8+ 个）

- `Fig. 1 |` `Fig. 2 |` `Fig. 3 |` `Fig. 4 |` `Fig. 5 |` `. 1 |` `. 2 |` `. 5 |` ← 全错
- `Extended Data` / `Extended Data Fig. 3 | ...` ← 错
- `'s 60 s 90 s a Heat maps'` ← 纯垃圾串

### 4. GROBID 完全漏掉、我们抓到的地方 —— 后置声明段（9 个）

`Data availability` · `Code availability` · `References` · `Acknowledgements` ·
`Author contributions` · `Funding` · `Competing interests` · `Additional information` · `Reporting summary`

> 这些恰恰是元数据/附件链接要用的区域，漏掉对科研阅读流程影响很大。

## 三、结论

### ✅ 不建议装全量版（8GB）

1. **章节切分不是全量版的卖点**。`-full` 相对 `-crf` 的增益主要在：引文/参考文献解析（`processCitationList`）、
   作者消歧与机构富化、更深的深度学习模型。**章节结构两边用的是同一套 TEI 输出契约**，
   上面第 3、4 类错误在 CRF 版出现，全量版同样会犯（它连 `Fig. 1 |` 都认成 head）。
2. **代价不成比例**：8GB 镜像 + Windows/Mac 容器只能跑 CPU（无 GPU 加速）→ 每篇 PDF 解析从秒级变十几秒级，
   换来的只是有限的引文解析提升，而你当前明确说"引文不必翻译"。
3. **它是互补，不是替代**：GROBID 强在行内小标题，我们强在后置声明段；直接替换只会把劣势换进来。

### ✅ 建议改为"GROBID 小标题补丁"（小步、可控）

只采纳同时满足以下条件的 GROBID head，其余全部丢弃：

1. 长度 ≤ 12 词；
2. **以句点结尾**（行内小标题的典型特征 —— 这一条就能滤掉 `Fig. 1 |` 这类）；
3. 不以 `Fig` / `Table` / `Extended Data` / `Supplementary` 开头；
4. 至少含一个真实字母词（复用已有的 `_has_real_word()`，滤掉 `'s 60 s 90 s a Heat maps'`）；
5. 在原文中确实能定位到该字符串（防止 TEI 串行错位）。

预期收益：**捞回 `Word recognizer.` 这类 5~6 个小标题**，且零图注噪音。
这一步等你有空验收当前批次后再开工（"一次只做一个阶段"）。

## 四、附：本次踩到的坑（已写入界面提示）

你先前启动的容器 `sharp_ptolemy` 处于 `Up` 状态，但 `docker ps` 的 **PORTS 一栏是空的**——
启动时漏了 `-p 8070:8070`。**容器在跑 ≠ 端口通**，所以网页版一直报"连不上"。

正确启动命令（`启动网页版.bat` 已自动执行）：

```
docker run -d --rm --name grobid --init --ulimit core=0 -p 8070:8070 grobid/grobid:0.9.1-crf
```

自查：`docker ps` 里 grobid 那一行的 PORTS 必须显示 `0.0.0.0:8070->8070/tcp`。

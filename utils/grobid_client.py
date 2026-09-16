"""
GROBID 客户端（阶段 4.3）
================================================================
本模块【不依赖 Streamlit】，可以脱离网页单独运行、单独测试。

它只做四件事：
    1. is_alive()         探测本地 GROBID 服务在不在（不在就安静降级，不报错）
    2. process_header()   把 PDF 字节流发给 GROBID 的 /api/processHeaderDocument，取回 TEI
    3. parse_tei_header() 把 TEI XML 解析成结构化字段（**纯函数**，可离线测试）
    4. parse_tei_heads()  从 TEI 里取出章节标题 <head>（同样是纯函数）——
                          给「小标题补丁」用，过滤规则见 utils/runin_headings.py

为什么要有它：
    阶段 4.1 / 4.2 的标题、作者、单位、摘要都是「本地规则 + 版面启发式」拼出来的，
    换一家出版社就可能失手。GROBID 是专门做论文头部信息抽取的 CRF/深度学习模型，
    把它当**第二意见**：两边结果一致 → 可信度大增；不一致 → 界面上并排显示，由人来定。

设计约定（与用户定下的原则一致）：
    · 服务没启动 / 超时 / 返回异常 → 一律抛 GrobidError（消息本身就是中文提示），
      调用方据此安静降级，**绝不阻塞阅读功能**；
    · consolidateHeader 默认 0：不让 GROBID 偷偷联网查 Crossref。
      一是避免网络依赖导致的不确定，二是保证对比的是「GROBID 自己的抽取能力」；
    · 解析写成**防御式**：命名空间无关、元素缺失不报错、作者优先取 titleStmt、
      缺失时回退 sourceDesc/biblStruct，避免不同 GROBID 版本/模型导致的字段位置差异。
"""

import re
import xml.etree.ElementTree as ET

import requests

# GROBID 容器默认端口（docker run -p 8070:8070）
DEFAULT_BASE_URL = "http://localhost:8070"
# 头部抽取实测 1~3 秒；首次调用要加载模型，所以给足超时
DEFAULT_TIMEOUT = 60.0
# 服务探测要快，不要卡住界面
ALIVE_TIMEOUT = 3.0


class GrobidError(Exception):
    """GROBID 调用失败。消息本身就是可以直接展示给用户的中文提示。"""


# ------------------------------------------------------------
# 服务探测
# ------------------------------------------------------------

def is_alive(base_url: str = DEFAULT_BASE_URL, timeout: float = ALIVE_TIMEOUT):
    """
    探测服务在不在。返回 (是否可用, 说明)。
    **不抛异常**——调用方拿它来决定「显示 GROBID 面板」还是「只显示一句未连接」。
    """
    url = base_url.rstrip("/") + "/api/isalive"
    try:
        response = requests.get(url, timeout=timeout)
    except requests.exceptions.ConnectionError:
        return False, f"连不上 GROBID 服务（{base_url}）：服务没启动，或容器没在跑。"
    except requests.exceptions.Timeout:
        return False, f"GROBID 服务响应超时（{base_url}）。"
    except Exception as exc:                       # 兜底：任何异常都不该打断阅读
        return False, f"探测 GROBID 服务出错：{type(exc).__name__}: {exc}"

    if response.status_code == 200 and response.text.strip().lower().startswith("true"):
        return True, "GROBID 服务正常"
    return False, f"GROBID 服务返回异常状态：HTTP {response.status_code}"


def server_version(base_url: str = DEFAULT_BASE_URL, timeout: float = ALIVE_TIMEOUT) -> str:
    """取 GROBID 版本号（用于界面显示与排错），失败返回空串"""
    url = base_url.rstrip("/") + "/api/version"
    try:
        response = requests.get(url, timeout=timeout)
        if response.status_code == 200:
            return response.text.strip()
    except Exception:
        pass
    return ""


# ------------------------------------------------------------
# 调用接口
# ------------------------------------------------------------

def process_header(pdf_bytes: bytes, base_url: str = DEFAULT_BASE_URL,
                   timeout: float = DEFAULT_TIMEOUT, consolidate: bool = False,
                   raw_affiliations: bool = True, filename: str = "input.pdf") -> bytes:
    """
    调用 /api/processHeaderDocument，返回原始 TEI XML 字节。

    consolidate=False  → consolidateHeader=0（不联网查 Crossref）
    raw_affiliations=True → includeRawAffiliations=1（保留原文单位字符串，比模型切碎的更可信）
    """
    url = base_url.rstrip("/") + "/api/processHeaderDocument"
    data = {
        "consolidateHeader": "1" if consolidate else "0",
        "includeRawAffiliations": "1" if raw_affiliations else "0",
    }
    files = {"input": (filename, pdf_bytes, "application/pdf")}
    try:
        response = requests.post(url, files=files, data=data, timeout=timeout)
    except requests.exceptions.ConnectionError:
        raise GrobidError(f"连不上 GROBID 服务（{base_url}）：请确认容器在运行"
                          "（docker run --rm --init -p 8070:8070 grobid/grobid:0.9.1-crf）。")
    except requests.exceptions.Timeout:
        raise GrobidError(f"GROBID 处理超时（>{timeout:.0f} 秒）。"
                          "首次调用要加载模型，可稍后重试；大文件也可能偏慢。")
    except Exception as exc:
        raise GrobidError(f"调用 GROBID 失败：{type(exc).__name__}: {exc}")

    if response.status_code != 200:
        detail = re.sub(r"\s+", " ", response.text or "")[:200]
        raise GrobidError(f"GROBID 返回 HTTP {response.status_code}：{detail}")
    return response.content


# ------------------------------------------------------------
# TEI 解析（纯函数，可离线测试）
# ------------------------------------------------------------

def _tag(element) -> str:
    """去掉命名空间前缀的标签名（GROBID 的 TEI 在 tei 命名空间下，但版本间偶有差异）"""
    return element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""


def _children(element, name: str):
    return [child for child in list(element) if _tag(child) == name]


def _descendants(element, name: str):
    return [node for node in element.iter() if _tag(node) == name]


def _text(element) -> str:
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip() if element is not None else ""


def _first_text(element, *path: str) -> str:
    """按「逐级子元素」路径取第一个非空文本，例如 _first_text(root, "teiHeader", "fileDesc")"""
    node = element
    for name in path:
        found = _children(node, name)
        if not found:
            return ""
        node = found[0]
    return _text(node)


def _compose_affiliation(node) -> str:
    """
    把一个 <affiliation> 拼成可读字符串。

    优先用 raw_affiliation 属性：它是 PDF 里的**原始单位字符串**
    （GROBID 的 includeRawAffiliations=1 会带上），比模型切碎的 orgName 更完整、
    也更方便人核对。没有属性时才用 orgName / address 拼。
    """
    raw = node.get("raw_affiliation") or node.get("rawAffiliation")
    if raw and raw.strip():
        return re.sub(r"\s+", " ", raw).strip(" ,;.")

    parts = []
    for org in _children(node, "orgName"):
        text = _text(org)
        if text and text not in parts:
            parts.append(text)
    for address in _children(node, "address"):
        for name in ("addrLine", "settlement", "region", "country"):
            for item in _children(address, name):
                text = _text(item)
                if text and text not in parts:
                    parts.append(text)
    return ", ".join(parts)


def _parse_person(author_node) -> dict:
    """解析一个 <author>（persName + email + affiliation + ORCID）"""
    person = {"name": "", "forename": "", "surname": "", "email": "",
              "orcid": "", "affiliations": []}

    pers_names = _children(author_node, "persName")
    if pers_names:
        node = pers_names[0]
        forenames = [_text(item) for item in _children(node, "forename") if _text(item)]
        surnames = [_text(item) for item in _children(node, "surname") if _text(item)]
        person["forename"] = " ".join(forenames)
        person["surname"] = " ".join(surnames)
        if person["forename"] or person["surname"]:
            person["name"] = f"{person['forename']} {person['surname']}".strip()
        else:
            person["name"] = _text(node)          # 兜底：直接取 persName 的文本

    for email in _descendants(author_node, "email"):
        text = _text(email)
        if text and not person["email"]:
            person["email"] = text

    for idno in _descendants(author_node, "idno"):
        if (idno.get("type") or "").upper() == "ORCID":
            person["orcid"] = _text(idno)

    for affiliation in _descendants(author_node, "affiliation"):
        text = _compose_affiliation(affiliation)
        if text and text not in person["affiliations"]:
            person["affiliations"].append(text)

    return person


def parse_tei_header(xml_bytes: bytes) -> dict:
    """
    把 GROBID 的 TEI 解析成结构化字段：

        {
          "title": str,
          "authors": [{name, forename, surname, email, orcid, affiliations}, …],
          "affiliations": [str],     # 全篇去重后的单位列表（按出现顺序）
          "abstract": str,
          "doi": str,
          "keywords": [str],
        }

    防御式解析：命名空间无关、元素缺失返回空值不报错。
    作者优先取 titleStmt（这是 header 抽取的主结果），
    titleStmt 里没有时才回退 sourceDesc/biblStruct/analytic —— 后者是参考文献式的描述，
    某些 GROBID 版本会把作者只放在这一处。
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise GrobidError(f"GROBID 返回的不是合法 XML（{exc}）。"
                          "多半是服务端出错或返回了错误页，请查看容器日志。")

    result = {"title": "", "authors": [], "affiliations": [],
              "abstract": "", "doi": "", "keywords": []}

    header = None
    for candidate in _descendants(root, "teiHeader"):
        header = candidate
        break
    if header is None:
        # 注意：错误页（例如 <html><body>Internal Server Error</body></html>）本身是**合法 XML**，
        # 只是没有 teiHeader。这种情况必须显式报错——否则界面会安静地显示一堆空字段，
        # 看起来像「GROBID 什么都没抽到」，实际是服务端出错。
        raise GrobidError("GROBID 返回的内容里没有 teiHeader（不是 TEI 结果，"
                          "多半是服务端返回了错误页）。请查看容器日志确认。")

    # ---- 标题 ----
    for title in _descendants(header, "title"):
        if (title.get("type") or "").lower() == "main" or (title.get("level") or "") == "a":
            result["title"] = _text(title)
            if result["title"]:
                break
    if not result["title"]:
        titles = _descendants(header, "title")
        result["title"] = _text(titles[0]) if titles else ""

    # ---- 作者：优先 titleStmt ----
    author_nodes = []
    for title_stmt in _descendants(header, "titleStmt"):
        author_nodes = _children(title_stmt, "author")
        if author_nodes:
            break
    if not author_nodes:
        for analytic in _descendants(header, "analytic"):
            author_nodes = _children(analytic, "author")
            if author_nodes:
                break

    seen = set()
    for node in author_nodes:
        person = _parse_person(node)
        key = (person["name"] or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        result["authors"].append(person)
        for affiliation in person["affiliations"]:
            if affiliation not in result["affiliations"]:
                result["affiliations"].append(affiliation)

    # ---- 摘要 ----
    for abstract in _descendants(header, "abstract"):
        paragraphs = [_text(item) for item in _descendants(abstract, "p") if _text(item)]
        result["abstract"] = " ".join(paragraphs) if paragraphs else _text(abstract)
        if result["abstract"]:
            break

    # ---- DOI ----
    for idno in _descendants(header, "idno"):
        if (idno.get("type") or "").upper() == "DOI":
            result["doi"] = _text(idno)
            if result["doi"]:
                break

    # ---- 关键词 ----
    for term in _descendants(header, "term"):
        text = _text(term)
        if text and text not in result["keywords"]:
            result["keywords"].append(text)

    return result


def header_from_pdf(pdf_bytes: bytes, base_url: str = DEFAULT_BASE_URL,
                    timeout: float = DEFAULT_TIMEOUT, consolidate: bool = False,
                    filename: str = "input.pdf") -> dict:
    """一步到位：发 PDF → 解析 → 返回结构化字段（服务不可用时抛 GrobidError）"""
    tei = process_header(pdf_bytes, base_url=base_url, timeout=timeout,
                         consolidate=consolidate, filename=filename)
    return parse_tei_header(tei)


# ------------------------------------------------------------
# 全文 TEI 里的章节标题 <head>（小标题补丁用）
# ------------------------------------------------------------

def parse_tei_heads(xml) -> list:
    """
    从 TEI 里取出所有 <head> 的文本（章节标题），按出现顺序返回、去重。

    **纯函数**，命名空间无关（沿用本模块的 _tag/_text 防御式解析）。

    关键的一条：**跳过祖先为 <figure> 的 <head>**。
    GROBID 把图注写成 `<figure><head>Fig. 1 | …</head>…</figure>`，
    结构上把它排除掉，就干掉了绝大多数噪音（实测 Nature 那篇的 8 条图注一次清空），
    而且不依赖任何字符串前缀规则——图注换一种写法（`. 1 |`）也照样排除。

    解析失败抛 GrobidError（与 parse_tei_header 一致，消息可直接展示给用户）。
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise GrobidError(f"GROBID 返回的不是合法 XML（{exc}）。"
                          "多半是服务端出错或返回了错误页，请查看容器日志。")
    return _tei_heads_in_order(root)


def _tei_heads_in_order(element, seen=None) -> list:
    """
    按文档顺序（深度优先前序）收集 <head> 文本；**<figure> 整棵子树直接跳过**。

    为什么要递归而不是 root.iter("head")：ElementTree 的节点没有父指针，
    用 iter() 拿到的 head 无法判断它在不在 <figure> 里；顺着树走一遍则天然知道。
    """
    if seen is None:
        seen = set()
    out = []
    for child in list(element):
        if _tag(child) == "figure":
            continue
        if _tag(child) == "head":
            text = _text(child)
            if text and text not in seen:
                seen.add(text)
                out.append(text)
        out.extend(_tei_heads_in_order(child, seen))
    return out


def heads_from_pdf(pdf_bytes: bytes, base_url: str = DEFAULT_BASE_URL,
                   timeout: float = DEFAULT_TIMEOUT, filename: str = "input.pdf") -> list:
    """
    调 /api/processFulltextDocument（全文解析），返回 parse_tei_heads 的结果。

    与服务探测的关系：**不在这个函数里再探一次**服务——调用方（webapp.state.run_grobid）
    已经先用 is_alive() 探过，这里直接发请求；服务没起来时 requests 会抛 ConnectionError，
    照样被翻译成同一条中文 GrobidError，界面安静降级即可。

    注意：全文解析比头部抽取慢（CRF 版实测 5~20 秒），所以超时沿用 60 秒，
    并且**只**用于「小标题补丁」这一个用途——不拿它替换现有的任何结果。
    """
    url = base_url.rstrip("/") + "/api/processFulltextDocument"
    files = {"input": (filename, pdf_bytes, "application/pdf")}
    try:
        response = requests.post(url, files=files, timeout=timeout)
    except requests.exceptions.ConnectionError:
        raise GrobidError(f"连不上 GROBID 服务（{base_url}）：请确认容器在运行"
                          "（docker run --rm --init -p 8070:8070 grobid/grobid:0.9.1-crf）。")
    except requests.exceptions.Timeout:
        raise GrobidError(f"GROBID 全文解析超时（>{timeout:.0f} 秒）。"
                          "首次调用要加载模型，可稍后重试；大文件也可能偏慢。")
    except Exception as exc:
        raise GrobidError(f"调用 GROBID 失败：{type(exc).__name__}: {exc}")

    if response.status_code != 200:
        detail = re.sub(r"\s+", " ", response.text or "")[:200]
        raise GrobidError(f"GROBID 返回 HTTP {response.status_code}：{detail}")
    return parse_tei_heads(response.content)


def summarize(parsed: dict) -> str:
    """把结果压成一行摘要，用于界面上的小字说明与日志"""
    authors = "、".join(person["name"] for person in parsed.get("authors", [])[:3]) or "（无作者）"
    if len(parsed.get("authors", [])) > 3:
        authors += " 等"
    return (f"GROBID：{len(parsed.get('authors', []))} 位作者（{authors}）· "
            f"{len(parsed.get('affiliations', []))} 条单位 · "
            f"摘要 {len(parsed.get('abstract', '').split())} 词 · "
            f"DOI {parsed.get('doi') or '无'}")

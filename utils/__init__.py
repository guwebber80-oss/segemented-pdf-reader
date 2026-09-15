"""
utils —— 阅读器的核心逻辑模块（阶段 5 从项目根目录收进包里）

分层约定（app.py 是唯一入口，只负责界面与流程编排）：

    解析层
        pdf_parser       PDF → 原子块 / 段落 / 卡片（含表格与公式的接线）
        formula_finder   公式三级判据与区域聚类
        table_finder     表格区域识别（题注锚点 + 对齐列几何校验）
        image_extractor  图片提取、卡片关联、区域截图渲染

    元数据层
        metadata         标题 / DOI / 摘要 / 作者 / 通讯作者 / 单位 / 附件链接
        grobid_client    GROBID 本地服务客户端（第二意见）
        metadata_compare 本地结果与 GROBID 的逐字段对照（只补空、不覆盖）

    翻译层
        translator       DeepL / OpenAI / Google 后端封装

包内模块之间用**相对导入**（`from . import formula_finder`），
外部（app.py、tests/）用 `from utils import pdf_parser`。
"""

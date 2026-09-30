# datasheet 固定样例（F19）

合成样例，**不是厂商原文**：只模仿 datasheet 的版式（逐页 Markdown + 目录），数值为虚构，用于离线测试入库、检索、读页与引用校验。
每个文件是 FixtureParser 能解析的 JSON：`{mpn, title, toc: [{title, page}], pages: [{page, text}]}`；文件内容的 SHA-256 即版本号。

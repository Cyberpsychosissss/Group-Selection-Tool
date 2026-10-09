"""纯逻辑层：不做任何 IO，不依赖 Web 框架和数据库。

- teams.py      互选关系 → 小队
- solver.py     池内重组方案（谁去哪个组）
- pairing.py    方案 → 交换搭档（谁接替谁）
- execution.py  执行阶段：此刻谁该在 LM Core 上做什么
"""

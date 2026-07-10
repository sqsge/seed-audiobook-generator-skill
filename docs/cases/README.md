# Case Plans / Case 分析方案

This directory stores case-level production plans for long-form audio-drama tests.

这个目录只放 case 级别的分析方案和生产设计，不放生成音频、不放临时运行日志，也不放模型返回的中间产物。

Each case plan should describe:

- source selection and production goal
- chapter/story-level adaptation strategy
- voice registry strategy
- music and ambience continuity
- chunking strategy
- ASR and QA gates
- output directory convention

每个 case 文档需要说明：

- 原文选择和制作目标
- 章节/剧情级改编策略
- 角色音色注册和复用策略
- 音乐、环境音和空间连续性策略
- chunk 切分策略
- ASR 与质量验收门禁
- 对应运行产物的目录规范

Current cases:

- `chapter28_full_chapter_audio_drama_plan.md`: full-chapter design for turning `Chapter 28: Flight of the Prince` into one continuous English audio-drama file.

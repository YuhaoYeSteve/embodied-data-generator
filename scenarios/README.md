# 场景模板

`object_sorting_60s.json` 是通用入门示例：4 段 × 15 秒，展示桌面几何道具的抓取、分类、放置与复核，适配 Seedance 2.0 和 Seedance 2.5。

`hotel_120s_v4.json` 以及 `hotel_120s_phone_reference.json` 的文件名为兼容原有本地模板保留，公开内容均为虚构的物品交接演示。每段 30 秒，须选择支持该单段时长的模型。带参考图的模板使用本项目绘制的 `refs/reference_board.png`，不包含客户图片。

可在生成中心导入并编辑 JSON。`expected_start_state`、`expected_end_state`、`required_actions` 和 `dataset` 是生成要求及计划标签；实际完成情况由成片检查判断，不是实测机器人轨迹或人工标注。

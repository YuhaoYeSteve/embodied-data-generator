# Contributing / 参与贡献

Contributions to task templates, model adapters, the workbench, tests, and documentation are welcome. For a substantial feature or behavior change, open a [feature request](https://github.com/YuhaoYeSteve/embodied-data-generator/issues/new?template=feature_request.yml) first so the design and scope can be discussed. Small fixes can go straight to a pull request.

## Local development

Use macOS, Linux, or WSL with Python 3.9+, Node.js 20, FFmpeg, and FFprobe. Fork the repository, clone your fork, then run:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-web.txt
python -B studio_server.py --simulate --port 8790 --open
```

Simulation runs the local workflow without credentials or model API calls. The frontend uses plain JavaScript and Node's built-in test runner; there is no npm install step. See the [README](README.en.md) for real-model configuration and the [workbench guide](WEB_STUDIO.md) for UI behavior.

## Validate a change

```bash
STUDIO_CONFIG_PATH=/tmp/embodied-data-generator-test-unconfigured.json python -B -m unittest discover -s tests -q
node --test tests/test_*.js
```

The path above should not exist, so tests use public defaults instead of personal configuration. CI runs backend and media tests on Python 3.9 and 3.12, plus frontend tests on Node.js 20. Tests use local media, simulation, and mocks; they do not require live model credentials.

Keep tests focused on the behavior you change. For UI changes, check the relevant workspace in simulation, including its loading, empty, and error states. Update both READMEs when public setup or capabilities change.

## Pull requests

Create a branch from `main` and keep each pull request focused. Explain the problem, resulting behavior, and verification you performed; include a screenshot when it makes a UI change easier to review. Link the related issue if there is one.

Never commit AK/SK, API keys, personal profiles, `studio.local.json`, signed media URLs, or private/generated runtime data. Use sanitized fixtures and public configuration examples. Review your staged diff before committing; ignored files are not a substitute for checking the content of a change.

Preserve the distinction between **planned** and **observed** data: script task/object/action labels use `annotation_scope: planned`; review observations and evidence stay separate. Do not present a planned label or generated video as verified sensor ground truth. Changes to recovery or review behavior should remain compatible with existing task and candidate records.

By submitting a contribution for inclusion, you agree to license it under the project's [Apache License 2.0](LICENSE). Only contribute material you have the right to share. Model services, third-party dependencies, and reference media retain their own terms.

## 中文说明

欢迎贡献任务模板、模型适配、页面交互、测试和文档。较大的功能或行为变更，请先通过 [功能建议](https://github.com/YuhaoYeSteve/embodied-data-generator/issues/new?template=feature_request.yml) 说明场景与范围；小修复可直接提交 PR。

按上面的命令启动模拟工作台、运行 Python 与前端测试，无需模型凭证。请从 `main` 创建分支，在 PR 中说明问题、修改后的行为与验证结果；涉及公开配置或能力变化时，同步修改中英文 README。

请勿提交 AK/SK、API Key、个人配置、带签名的媒体地址及私有运行数据。使用脱敏测试样本，并检查暂存差异。脚本计划标签必须保留 `annotation_scope: planned`，与质检观察和证据分开，不能当作传感器或逐帧标注真值。提交的贡献遵循项目的 [Apache-2.0 许可证](LICENSE)，并应具有合法分享权利。

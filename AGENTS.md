# AGENTS.md

## 项目要求

1.本项目固定使用本地Python3.11。
2.必须使用项目根目录下的.venv虚拟环境。
3.禁止将依赖安装到系统Python环境。
4.每次只完成一个明确的小任务，不要一次性开发全部功能。
5.每轮修改完成后必须测试。
6.只要产生文件修改，每次对话结束前必须执行Git提交。
7.不得删除或覆盖用户已有代码。
8.不得提交密码、Token、Cookie、.env等敏感信息。
9.不得伪造测试结果或Git提交结果。

## 环境检查

开始任务前执行：

```bash
python --version
git status
```

Python版本必须为3.11。

如果不存在.venv，执行：

```bash
py -3.11 -m venv .venv
```

Windows激活虚拟环境：

```bash
.venv\Scripts\activate
```

安装依赖必须使用：

```bash
python -m pip install 包名
```

依赖变化后更新：

```bash
python -m pip freeze > requirements.txt
```

## 开发流程

每轮任务按照以下顺序执行：

1.阅读AGENTS.md和现有代码。
2.确认本轮唯一开发目标。
3.检查Git状态和Python环境。
4.只修改与当前任务有关的文件。
5.执行语法检查、测试或实际运行验证。
6.检查git diff和git status。
7.执行Git提交。
8.汇报修改文件、测试结果和提交信息。

## 测试要求

至少执行：

```bash
python -m compileall .
```

如果项目使用pytest，执行：

```bash
python -m pytest
```

测试失败时必须先修复，不能直接提交并声称完成。

## Git要求

产生修改后必须执行：

```bash
git add -A
git commit -m "feat: 本轮完成内容"
```

提交信息示例：

```text
feat: 增加浏览器启动功能
fix: 修复登录状态检测
docs: 更新项目说明
chore: 初始化项目结构
```

禁止擅自执行：

```text
git reset --hard
git clean -fd
git push --force
```

## 每轮完成后汇报

每次任务结束后说明：

1.本轮完成内容。
2.修改的文件。
3.执行的测试。
4.测试结果。
5.Git提交信息。
6.Git提交哈希。
7.下一步建议。

## 最高优先级

* 固定使用Python3.11。
* 必须使用.venv虚拟环境。
* 每次只完成一个小目标。
* 修改后必须测试。
* 产生修改后必须Git提交。
* 不得破坏现有代码。
* 不得伪造结果。

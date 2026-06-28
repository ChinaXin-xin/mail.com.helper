# mail.com.helper 邮件管理工具

`mail官网`：https://www.mail.com

由于mail.com免费账号不支持IMAP和POP3协议，无法通过传统方式批量接收邮件，网上也缺少专门针对mail.com的批量邮箱管理和内容格式化工具，因此我开发了这个项目。

<img width="2230" height="1130" alt="image" src="https://github.com/user-attachments/assets/3fdcf257-2be1-47d0-9ba2-314ba8edf6b6" />
<img width="3072" height="1817" alt="image" src="https://github.com/user-attachments/assets/f501b263-707f-4998-80f3-a336d25df869" />

## 项目介绍

本项目是一个面向本地邮件GUI管理软件，使用Python模拟mail.com的网页邮箱流程完成登录、收件箱读取和邮件详情解析，不依赖浏览器自动化。软件支持批量导入账号、并发收取、账号多选刷新或删除、本地会话恢复，以及每 5 秒自动拉取新邮件。界面右侧可查看邮件样式，账号任务支持右键复制邮箱。程序还提供 `127.0.0.1:8913` 本地接口，可按邮箱、关键词和正则表达式查询验证码等内容；实时接口可用账号密码临时登录查询，未匹配时返回 `NullX`。整体思路是把邮箱读取、内容解析、缓存展示。

## 项目环境
- Python：Python 3.11
- 启动文件：`start.py`
- 本地接口端口：`8913`

## 运行方式

```powershell
.\.venv\Scripts\python.exe start.py
```

## 接口示例

查询已缓存邮件：

```text
http://127.0.0.1:8913/search-mail?email=weatherallmayalyn761@mail.com&keyword=ChatGPT&regex=\d{6}
```

使用账号密码实时查询：

```text
http://127.0.0.1:8913/search-mail-live?email=weatherallmayalyn761@mail.com&password=xxx&keyword=ChatGPT&regex=\d{6}
```

返回值为匹配到的字符串；没有匹配到时返回：

```text
NullX
```

## 打包说明

推荐使用 PyInstaller 在 `.venv` 中打包：

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconsole --onefile --name ccGptMail --icon C:\Users\xin\Pictures\ico64.ico start.py
```

生成文件位于：

```text
dist\ccGptMail.exe
```

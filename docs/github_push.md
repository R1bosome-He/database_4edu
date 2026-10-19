# 使用自己的 GitHub 身份提交与推送

## 当前状态

- 工作目录：`C:\Users\16651\Desktop\资料夹\复习资料\数据库原理及安全\dbms_project`。
- 分支：`c-module-integration`；origin：`https://github.com/R1bosome-He/database_4edu.git`。
- 保留共同历史基线 `a567eff`；新版已提升到根目录，旧版在 `legacy/`，C 实现已保留。
- 2026-10-20 两次读取 GitHub 失败（连接重置／无法连接 443），暂不能确认该基线之后是否有更新，后续必须先 fetch。
- 初次目录整理只准备本地改动。随后用户已创建提交 006863e，并成功 fetch 至 d7c7dee；本次解决 rebase 冲突，保留双方代码，线上尚未推送。

## 1. 设置提交作者

GitHub 登录决定是否有权上传；user.name / user.email 决定提交作者显示。配置作者不等于登录或获得写权限。

```powershell
cd "C:\Users\16651\Desktop\资料夹\复习资料\数据库原理及安全\dbms_project"
git config user.name "你的显示名称"
git config user.email "GitHub已验证邮箱或GitHub提供的noreply邮箱"
```

只修改本仓库配置；邮箱从自己的 GitHub Settings → Emails 获取，不照抄其他人身份。

## 2. 用浏览器登录

本机已安装 Git for Windows 和 Git Credential Manager（GCM）；未检测到 gh 命令，不必额外安装它。

```powershell
git credential-manager github login --username "你的GitHub用户名" --browser
```

在浏览器中确认自己的账号，完成授权及双重验证。若不方便自动打开浏览器，可改用设备码流程：

```powershell
git credential-manager github login --username "你的GitHub用户名" --device
```

仅在自己的浏览器中输入终端给出的临时代码。不向聊天发送密码、Token 或设备码。GCM 将凭据保存在 Windows 凭据管理器。

`git credential-manager github list` 可列出已知账号；受限终端可能无法读取凭据，本次未据此判定你已登录或未登录。

## 3. 后续准备好再上传

> 2026-10-20 后续状态：本地已执行 commit，并处理到 d7c7dee 的 rebase；以下首次提交步骤保留为流程说明，不要再次照抄旧基线命令。以 `git status` 和最新 `git log` 为准。rebase 完成、测试通过后，推送当前 c-module-integration 分支即可。

以下提交及推送命令本次不会代为执行。先恢复远端连接，检查是否有新提交：

```powershell
git fetch origin
git log --oneline a567eff..origin/main
git status
git diff --cached --stat
```

如果远端已有更新，先检查涉及 A/B/D 的修改。确认内容和作者后保存本地提交并整合最新 main：

```powershell
git add -A
git commit -m "Promote modular DBMS, archive legacy and add C module"
git rebase origin/main
```

rebase 如有冲突，结合新路径保留队友修改；不确定时可 `git rebase --abort` 返回整合前。再次运行测试后上传同名分支：

```powershell
git push -u origin c-module-integration
```

在 GitHub 创建 Pull Request，将此分支合入 main。合入后默认仓库页面才显示新目录结构，不使用强制 push 覆盖 main。

如果没有写权限，需要仓库所有者邀请为 collaborator 并接受邀请；或 fork 后提交 PR。仅登录账号不会获得仓库写权限。

## 官方说明

- [GitHub：GCM 凭据与浏览器登录](https://docs.github.com/en/get-started/git-basics/caching-your-github-credentials-in-git)
- [GitHub：提交作者名称](https://docs.github.com/en/get-started/git-basics/setting-your-username-in-git)

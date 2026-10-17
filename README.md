# 表情包小抽屉 · Sticker Shelf

> 一格放你自己表情包的小抽屉，和你的 AI（下面叫「小机」）一起用。
> 你上传、起名字、写清它的含义和使用时机、决定哪些小机能用；小机发 `[sticker:编号]`，你的聊天界面把它变成图；
> 你写的描述就是语义索引：小机会依据当前对话，从名字和描述里挑合适的表情包。
>
> A drawer for your own stickers, shared with your AI companion. Upload them, name them, choose which ones your AI may use.
> Your AI sends `[sticker:id]`, your chat UI turns it into the image; the stickers you send reach the AI as their names,
> so it understands what you meant.

![预览 · Preview](docs/preview.png)

---

## 一、怎么跑 · Setup

需要电脑上有 **Python 3.9 以上**，不用装任何别的东西：
- **macOS**：系统自带。
- **Windows**：到 [python.org](https://www.python.org/downloads/) 下载安装，**安装第一屏记得勾上 “Add python.exe to PATH”**。

下载源码，双击 `run.command`（Mac，第一次可能要右键 → 打开）或 `run.bat`（Windows），
浏览器打开 **http://127.0.0.1:8767** 就是你的抽屉。关掉那个黑窗口就停了。

Download the source, double-click `run.command` (macOS) or `run.bat` (Windows), then open http://127.0.0.1:8767.
Python 3.9+ standard library only.

---

## 二、怎么用 · Using it

1. **上传**：点「选图片上传」，或者把图片拖进来（可以一次好几张）。支持 PNG / JPG / GIF / WebP，单张不超过 5 MB，最多 300 张。**GIF 原样保存，会动。**
2. **起名字**：名字写成动作或情绪，2–10 个字最好，比如「委屈巴巴」「笑到打滚」「摸摸头」。图上有字幕就直接用字幕原话。点卡片上的名字随时可以改。
3. **写描述**：用自然语言补一句“它是什么意思、什么时候用”，例如「对方委屈时温柔安慰，表示抱住不放」。这句话会进入 AI 的表情包清单，作为按当前对话选择表情的语义索引；点卡片上的描述随时可以改。
4. **小机能用**：每张卡片上有个开关。关掉的，小机看不到、也不会发；你自己照样能发。
5. **试一试**：页面最下面能贴一段小机的回复，预览它在聊天里的样子，也能看到小机每轮收到的表情包清单。

---

## 三、接到 ChatGPT 官端（私人 MCP 插件）

云端版现在可以直接作为 ChatGPT 的私人插件使用。它不会搬动聊天记录，也不会创建新的 AI 身份；
插件只提供两件只读能力：读取允许 AI 使用的表情描述，以及在对话里显示选中的图片/GIF。

部署时除了上一节的变量，再添加：

- `STICKER_MCP_TOKEN`：至少 16 位、只包含字母、数字、`_` 或 `-` 的随机字符串。它会成为私人 MCP 地址的一部分。
- `STICKER_MEDIA_SECRET`：另一段随机长密码，用来给聊天里的图片地址签名。设置后不要随意更换，否则旧消息里的图片会失效。

插件地址为：

```
https://你的域名/mcp/你的_STICKER_MCP_TOKEN
```

在 ChatGPT 网页端进入 Plugins，选择 **Add custom MCP server**，填入上面的地址并选择 No authentication；
安装后回到原来的普通对话，在输入框输入 `@` 并选择「表情包小抽屉」。第一次可以说：

> 以后在合适的情绪和关系语境里，可以调用表情包小抽屉；每轮最多一张，不要解释表情名称，也不要为了发而发。

插件不会向服务端发送完整聊天记录。模型只会在需要找表情时传入一句不超过 500 字的当前语境概括；
服务端返回你自己写的名字和描述，由模型选择，再用内嵌卡片显示图片或 GIF。

MCP 端到端测试：`npm run test:mcp`

---

## 四、接到自建聊天前端 / 网关 · Wiring it to your own AI

这是给**自己搭了聊天前端 / 网关**的人用的。抽屉开着的时候，有三条接口：

```
GET  /stickers/context     每轮接在系统提示末尾：发表情包的规矩 + 小机能用的语义清单（编号＝名字＋描述）
POST /stickers/expand      {"text": "...", "role": "user" | "assistant"} → {"text": "..."}
                           聊天记录交给小机之前，每条过一遍
POST /stickers/normalize   {"text": "..."} → {"text": "..."}
                           小机的回复存下来之前过一遍
GET  /stickers/img/<编号>   图片本身
```

一轮对话大概是这样（Python 示例，别的语言同理）：

```python
import json, urllib.request

SHELF = "http://127.0.0.1:8767"

def shelf_get(path):
    with urllib.request.urlopen(SHELF + path) as response:
        return response.read().decode("utf-8")

def shelf_post(path, payload):
    request = urllib.request.Request(SHELF + path, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())["text"]

# 1. 系统提示末尾接上表情包清单
system_prompt = your_system_prompt + "\n\n" + shelf_get("/stickers/context")

# 2. 聊天记录里的表情包换成小机看得懂的样子
history = [{"role": m["role"], "content": shelf_post("/stickers/expand", {"text": m["content"], "role": m["role"]})}
           for m in history]

# 3. 照常调用你的模型，拿到回复后整理一下再存
reply = shelf_post("/stickers/normalize", {"text": reply})
```

**为什么这么设计：**
- 你发的表情包变成【表情包：名字】，小机才知道你在撒娇还是在生气；
- 小机**自己**发过的表情包保留 `[sticker:编号]` 原样——如果连它自己的也换成文字，它会开始学着写「【表情包：…】」这种字，而不是发真的表情包；
- 小机偶尔还是会把表情包写成文字、或者发完又念一遍名字，`normalize` 会把它们改回真表情包；
- 删掉的表情包在记录里变成【一张已删除的表情包】，小机不会再拿那个编号去发。

**在聊天界面里显示：** 把 `[sticker:编号]` 换成图片就行。抽屉自带一个小脚本，直接引用：

```html
<script src="http://127.0.0.1:8767/render.js"></script>
<script>
  // 文字按纯文本放进去，不会被注入 HTML
  StickerShelf.render(messageElement, messageText, "http://127.0.0.1:8767");
</script>
```

你自己发表情包时，消息内容写成 `[sticker:编号]` 就好（编号在每张卡片上，可以直接复制）。

> 这套是给自建前端用的。Claude 桌面 App 之类的官方聊天界面显示不了自定义图片，所以没有做成扩展。

---

## 五、自动起名（可选）· Auto names (optional)

不填名字时，可以让 Claude 看图起一个：

```
pip install anthropic
```

再设置环境变量 `ANTHROPIC_API_KEY`（或者用 `ant auth login` 登录过也行），重新打开抽屉，页面上会提示自动起名已开启。
默认用 `claude-opus-5-5`，想换别的模型设 `STICKER_LABEL_MODEL`。
GIF 只会被看到第一帧，动作在后面几帧才出现的，名字可能不准，点名字改一下就好。

没装、没设置也完全没关系，只是名字要自己填。

---

## 六、你的表情包放在哪 · Your data

全在 `data/` 里：`data/stickers.json`（名字、描述和开关）和 `data/images/`（原图）。
它们只在你自己电脑上，不在这个仓库里（`.gitignore` 已经排除了）。换电脑时把整个 `data/` 拷过去就行。

### 放到云端，手机也能用（Railway）

推荐把**程序代码**放在 GitHub，把**私人表情包**放在 Railway 的持久存储卷里。这样手机只要打开一个网址、输入密码，就能上传、修改描述和导出备份；图片不会提交到 GitHub。

1. 在 Railway 选择 **Deploy from GitHub repo**，连接这个仓库。
2. 给服务添加一个 Volume，挂载路径填 `/data`。
3. 添加这些变量：
   - `STICKER_DATA_DIR=/data`
   - `STICKER_ADMIN_PASSWORD=你自己设置的管理密码`（至少 12 个字符，不要写进 GitHub）
   - `STICKER_API_TOKEN=另一个随机长密码`（可选；以后给聊天程序调用接口时使用）
4. 生成 Railway 的公开域名，用手机浏览器打开。第一次会先进入密码页。
5. 定期点页面右上角的「导出备份」，会下载包含原图和描述的 ZIP。

云端模式会强制要求管理密码；没有登录或正确的 API Token，就不能查看图片和语义清单。免费额度和是否需要付款方式以 Railway 当时页面显示为准：如果平台要求绑卡或升级套餐，先不要继续。

聊天程序调用云端接口时，把 API Token 放在请求头里：

```http
Authorization: Bearer 你的_STICKER_API_TOKEN
```

不要把管理密码、API Token 或导出的备份提交到 GitHub。

测试：`python -m unittest discover tests`

---

## 许可 · License

程序代码采用 **PolyForm Noncommercial License 1.0.0**（见 [LICENSE](LICENSE)）：可以用、可以改、可以分享，**不能商用**。
你上传的表情包是你自己的，不在这个仓库里，也不受本许可约束——发到网上之前，记得表情包图片多半有原作者。

Program code is licensed under the PolyForm Noncommercial License 1.0.0. The stickers you upload stay on your machine and are yours.

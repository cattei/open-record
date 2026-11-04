"""
飞书机器人「一键创建」引导
==========================
飞书开放平台**没有**提供「创建自建应用」的服务端 API（lark_oapi 的 application
资源只有 get / list / patch / underauditlist，v7 也只有 patch 与 publish）。
所以任何宣称「点一下就能在飞书里建出机器人」的实现，本质都是**引导用户去开发者
后台建**，再把他拿到的凭据接回本机。本模块把这条路做顺：

  1. 直接打开「创建企业自建应用」页面，并把建议的应用名放进剪贴板，省去手打
  2. 列出必须开通的权限，并能一键打开权限申请页
  3. 用户拿到 App ID / App Secret 后粘贴回来
  4. 调 tenant_access_token 接口**真的校验一次**，避免「填了但没生效」
  5. 校验通过才写配置并启用，失败给出人话错误

第 4 步是关键：原来只有「填了 App ID 和 Secret 就算启用」，凭据写错、复制漏了
一位、权限没开，用户都只会在之后收不到消息时才发现，而且不知道错在哪。
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger("feishu_setup")

# 开发者后台入口。这些是飞书官方页面，链接变更时只需改这里。
CONSOLE_URL = "https://open.feishu.cn/app"
CREATE_URL = "https://open.feishu.cn/app?from=create"          # 创建应用页
PERMISSION_URL = "https://open.feishu.cn/app/{app_id}/auth"     # 该应用的权限管理页
TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"

# 长连接收消息 + 回消息所需的最小权限集。
# im:message 负责接收消息事件；im:message:send_as_bot 负责以机器人身份发消息。
REQUIRED_SCOPES = (
    ("im:message", "接收消息（读取用户发给机器人的消息）"),
    ("im:message:send_as_bot", "以机器人身份发送消息（回指令、发录音文件）"),
)

# 默认应用名，写进剪贴板方便用户直接粘贴。带日期后缀避免和已有应用重名。
DEFAULT_APP_NAME = "录音助手"


@dataclass
class VerifyResult:
    """凭据校验结果。ok=False 时 error 一定是能直接展示给用户的中文句子。"""

    ok: bool
    error: str = ""
    app_name: str = ""
    scopes: list[str] = field(default_factory=list)


def looks_like_app_id(s: str) -> bool:
    """飞书 App ID 形如 cli_a1b2c3d4e5f6g7h8，统一 cli_ 前缀。

    只做「像不像」的本地预检，真正的判定交给服务端校验 —— 不在这里猜格式，
    因为飞书历史上改过长度，硬编码位数会把合法 ID 判死。
    """
    s = (s or "").strip()
    return s.startswith("cli_") and len(s) >= 12


def _http_post_json(url: str, payload: dict, timeout: float = 8.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def verify_credentials(app_id: str, app_secret: str, timeout: float = 8.0) -> VerifyResult:
    """用 tenant_access_token 接口校验 App ID / Secret 是否有效。

    这是飞书鉴权的第一步：凭据错会直接返回 code != 0，不需要任何业务权限，
    所以可以拿它当「凭据自检」。整个过程不落盘、不打印密钥。

    校验能区分三类问题，这对用户很重要：
      · 10003 / 10014 之类 → ID 或 Secret 写错
      · 网络异常          → 断网或代理，不是填错
      · 其它 code         → 应用被停用/权限问题，原样透出 code 与 msg
    """
    app_id = (app_id or "").strip()
    app_secret = (app_secret or "").strip()

    if not app_id or not app_secret:
        return VerifyResult(False, "App ID 和 App Secret 都要填写。")
    if not looks_like_app_id(app_id):
        return VerifyResult(
            False,
            f"App ID 看起来不对：应以 cli_ 开头，当前填的是「{app_id[:12]}…」。\n"
            "请到开发者后台「凭证与基础信息」里重新复制。",
        )

    try:
        body = _http_post_json(
            TOKEN_URL, {"app_id": app_id, "app_secret": app_secret}, timeout=timeout
        )
    except urllib.error.HTTPError as e:
        # 4xx/5xx 也带 json body，尽量把飞书的原因读出来
        try:
            detail = json.loads(e.read().decode("utf-8"))
            code, msg = detail.get("code"), detail.get("msg")
        except Exception:
            code, msg = e.code, str(e)
        return VerifyResult(False, f"飞书返回错误（code={code}）：{msg}")
    except urllib.error.URLError as e:
        return VerifyResult(
            False,
            f"连不上飞书开放平台：{e.reason}。\n"
            "请检查网络或代理设置，这不是凭据填写的问题。",
        )
    except TimeoutError:
        return VerifyResult(False, "连接飞书超时（8 秒）。请检查网络后重试。")
    except Exception as e:
        log.exception("校验凭据时出现未预期异常")
        return VerifyResult(False, f"校验时出错：{type(e).__name__}: {e}")

    code = body.get("code")
    if code != 0:
        msg = body.get("msg") or ""
        hint = ""
        # 常见错误码给一句人话，避免用户拿着数字去搜
        if code in (10003, 10014, 10001):
            hint = "\n多半是 App ID 与 App Secret 不匹配，或复制时漏了字符。"
        elif code in (10004, 10005):
            hint = "\n应用可能已停用，或被管理员限制，请到后台确认应用状态。"
        return VerifyResult(False, f"凭据无效（code={code}）：{msg}{hint}")

    return VerifyResult(True)


def apply_to_config(cfg: dict, app_id: str, app_secret: str) -> dict:
    """把校验通过的凭据写进 cfg（就地修改并返回），保持 enabled 语义一致。

    enabled 的含义是「开机就建立长连接」。这里要求 id 与 secret 都在，
    和 gui.on_save 里的判定保持同一条规则，避免两处逻辑再次漂移。
    """
    fs = cfg.setdefault("feishu", {})
    fs["app_id"] = (app_id or "").strip()
    fs["app_secret"] = (app_secret or "").strip()
    fs["enabled"] = bool(fs["app_id"] and fs["app_secret"])
    return cfg


def verify_async(app_id: str, app_secret: str, on_done) -> threading.Thread:
    """后台线程校验，回调在主线程由调用方用信号转接。

    校验是一次网络请求，最长 8 秒。直接在主线程调会把窗口卡住（点按钮后
    界面无响应），所以一律走线程。
    """

    def work():
        try:
            res = verify_credentials(app_id, app_secret)
        except Exception as e:                      # 兜底，绝不让线程静默死掉
            log.exception("校验线程异常")
            res = VerifyResult(False, f"校验异常：{type(e).__name__}: {e}")
        try:
            on_done(res)
        except RuntimeError:
            # 窗口已销毁（用户校验中途退出），回调 emit 会抛这个，属正常情况
            pass

    t = threading.Thread(target=work, name="feishu-verify", daemon=True)
    t.start()
    return t


def guide_text(app_name: str) -> str:
    """向导正文。写成用户能照着点的步骤，不堆术语。"""
    steps = "\n".join(f"    • {s}" for s in (f"{c}  ——  {d}" for c, d in REQUIRED_SCOPES))
    return (
        "飞书没有开放「用接口创建应用」，所以这一步由你在浏览器里点几下完成，"
        "本程序负责把剩下的都接好。\n\n"
        "第 1 步　浏览器已打开飞书开发者后台，点「创建企业自建应用」\n"
        f"　　　　应用名已复制到剪贴板，直接粘贴即可：{app_name}\n\n"
        "第 2 步　建好后，在「添加应用能力」里启用「机器人」\n\n"
        "第 3 步　在「权限管理」里开通这两项：\n"
        f"{steps}\n\n"
        "第 4 步　回到「凭证与基础信息」，复制 App ID 和 App Secret\n"
        "　　　　粘贴到下面两个输入框，然后点「校验并启用」\n\n"
        "校验会真的连一次飞书服务器。凭据填错、应用停用、断网，都会当场告诉你原因。"
    )

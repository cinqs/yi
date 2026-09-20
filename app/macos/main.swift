// yi.app —— macOS 壳
//
// 只做三件事：起后端 agent、开一个窗口、把 agent 的界面装进去。
// 代理内核是 mihomo（成熟开源），买机器/装服务端是 yi（本项目），
// 这个壳本身不含任何网络逻辑——所以它短、也几乎不可能出安全问题。

import Cocoa
import WebKit

// 构建脚本会把这两个替换成**构建机器上的**绝对路径。
//
// 只靠它们是不行的：那样打出来的 .app 换台机器、换个目录就废了
// （Release 里那个 zip 就是这个问题——里面写的是 CI runner 上的路径）。
// 所以打包时会把 agent、界面、yi 包的源码一起塞进 Contents/Resources/，
// 运行时按下面的顺序去找：源码目录优先（开发者改完立刻生效），
// 找不到再用自带的那份（别人下载下来也能直接跑）。
let BAKED_PYTHON = "__PYTHON__"
let BAKED_AGENT = "__AGENT__"
let AGENT_PORT = 8765

/// 运行时的路径解析。集中放这里，免得散落在各处靠猜。
enum Runtime {
    static let fm = FileManager.default

    static func exists(_ path: String?) -> String? {
        // 不用 `guard let path` 的简写形式：那是 Swift 5.7+ 的语法，
        // 而这里只保证有 CommandLineTools 自带的 swiftc（版本可能更老）。
        guard let path = path, fm.fileExists(atPath: path) else { return nil }
        return path
    }

    /// agent 脚本：先认构建时写死的源码路径（开发时改完就生效），
    /// 再认 App 自带的副本（发布出去只有这一份）。
    static var agentPath: String? {
        if let live = exists(BAKED_AGENT) { return live }
        guard let res = Bundle.main.resourcePath else { return nil }
        return exists(res + "/agent.py")
    }

    /// 用自带 agent 时，要顺手把 PYTHONPATH 指向自带的 yi 包。
    /// 用源码目录那份时不用管——agent.py 自己会把 ../src 加进 sys.path。
    static var agentEnvironment: [String: String] {
        var env = ProcessInfo.processInfo.environment
        if exists(BAKED_AGENT) == nil, let res = Bundle.main.resourcePath {
            env["PYTHONPATH"] = res + "/src"
        }
        return env
    }

    /// 找一个能用的解释器。
    ///
    /// 门槛是 **3.11**（agent 要用 `tomllib`），不是随便一个 python3 ——
    /// macOS 自带的 `/usr/bin/python3` 是 3.9，装上会以一个莫名其妙的
    /// ImportError 收场，所以必须真的探一次版本。
    static var pythonPath: String? {
        var candidates: [String] = []
        if let res = Bundle.main.resourcePath {
            candidates.append(res + "/python/bin/python3")   // 将来若捆绑解释器
        }
        candidates.append(BAKED_PYTHON)
        candidates.append(contentsOf: [
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            NSHomeDirectory() + "/.local/bin/python3.12",
            NSHomeDirectory() + "/.local/bin/python3",
            NSHomeDirectory() + "/.local/share/uv/python",
            "/usr/bin/python3",
        ])
        if let found = which("python3") { candidates.append(found) }
        if let found = which("python3.12") { candidates.append(found) }

        for c in candidates where isUsablePython(c) { return c }
        return nil
    }

    static func isUsablePython(_ path: String) -> Bool {
        guard fm.isExecutableFile(atPath: path) else { return false }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: path)
        task.arguments = ["-c", "import sys, tomllib; sys.exit(0 if sys.version_info >= (3, 11) else 1)"]
        task.standardOutput = FileHandle.nullDevice
        task.standardError = FileHandle.nullDevice
        do { try task.run() } catch { return false }
        task.waitUntilExit()
        return task.terminationStatus == 0
    }

    /// GUI 应用拿到的是精简版 PATH，`which` 常常找不到 Homebrew 里的东西，
    /// 所以自己给它一个更宽的 PATH。
    static func which(_ name: String) -> String? {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/which")
        task.arguments = [name]
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + NSHomeDirectory()
            + "/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        task.environment = env
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = FileHandle.nullDevice
        do { try task.run() } catch { return nil }
        task.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let out = String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return (out?.isEmpty == false) ? out : nil
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var agent: Process?
    var statusItem: NSStatusItem?

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        startAgent()

        let frame = NSRect(x: 0, y: 0, width: 960, height: 700)
        window = NSWindow(
            contentRect: frame,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "yi"
        window.center()
        window.setFrameAutosaveName("yi-main")

        let config = WKWebViewConfiguration()
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")
        webView = WKWebView(frame: frame, configuration: config)
        webView.navigationDelegate = self
        // 必须设置 uiDelegate：WKWebView 默认**不显示** alert/confirm/prompt，
        // 不实现这些回调时 JS 会静默卡住（页面按钮就像坏了一样）。
        webView.uiDelegate = self
        webView.setValue(false, forKey: "drawsBackground")
        window.contentView = webView
        window.makeKeyAndOrderFront(nil)

        // agent 可能要一两秒才起来，先等一下再加载
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            self.loadUI()
        }
    }

    func loadUI() {
        // 起不来的时候要给一个能照着做的页面，而不是一片空白窗口
        // —— 空白窗口只会让人以为是 App 坏了。
        if let problem = startupError {
            webView.loadHTMLString(Self.errorPage(problem), baseURL: nil)
            return
        }
        guard let url = URL(string: "http://127.0.0.1:\(AGENT_PORT)/") else { return }
        webView.load(URLRequest(url: url))
    }

    var startupError: String?

    func startAgent() {
        // 已经在跑就不重复起（比如命令行起的那个）
        if agentIsUp() { return }

        guard let agentPath = Runtime.agentPath else {
            startupError = "App 里没找到 agent.py。<br>多半是打包不完整，重新跑一次 <code>make app</code>。"
            NSLog("yi: 找不到 agent 脚本")
            return
        }
        guard let python = Runtime.pythonPath else {
            startupError = """
            找不到 Python 3.11 以上的解释器。<br><br>
            「驿」需要 Python 3.12 来跑后台。<br>
            装一个再重开：<code>brew install python@3.12</code><br>
            或者用 uv：<code>curl -LsSf https://astral.sh/uv/install.sh | sh</code>
            """
            NSLog("yi: 找不到可用的 Python")
            return
        }

        let task = Process()
        task.executableURL = URL(fileURLWithPath: python)
        task.arguments = [agentPath, "--port", String(AGENT_PORT)]
        task.environment = Runtime.agentEnvironment
        // agent 的输出写文件，别丢进 /dev/null —— 出问题时那是唯一的线索
        let logDir = NSString(string: "~/.config/yi/logs").expandingTildeInPath
        try? FileManager.default.createDirectory(
            atPath: logDir, withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700])
        let logPath = (logDir as NSString).appendingPathComponent("agent.log")
        if !FileManager.default.fileExists(atPath: logPath) {
            FileManager.default.createFile(atPath: logPath, contents: nil)
        }
        if let handle = FileHandle(forWritingAtPath: logPath) {
            handle.seekToEndOfFile()
            task.standardOutput = handle
            task.standardError = handle
        } else {
            task.standardOutput = FileHandle.nullDevice
            task.standardError = FileHandle.nullDevice
        }
        do {
            try task.run()
            agent = task
        } catch {
            NSLog("yi: 启动 agent 失败 \(error)")
        }
    }

    func agentIsUp() -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(AGENT_PORT)/api/state") else { return false }
        let semaphore = DispatchSemaphore(value: 0)
        var up = false
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: request) { _, response, _ in
            if let http = response as? HTTPURLResponse, http.statusCode == 200 { up = true }
            semaphore.signal()
        }.resume()
        _ = semaphore.wait(timeout: .now() + 2)
        return up
    }

    /// 后端起不来时显示的页面。用和主界面同一套令牌，别让人以为点进了别的 App。
    static func errorPage(_ message: String) -> String {
        """
        <!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
        <style>
          :root { --bg:#0a0c11; --surface:#12151d; --line:#242a36;
                  --text:#eef1f6; --dim:#8b93a3; --warn:#ffb454; }
          body { margin:0; height:100vh; display:grid; place-items:center;
                 background:radial-gradient(700px 380px at 50% -10%, rgba(255,180,84,.08), transparent 62%), var(--bg);
                 color:var(--text); font:15px/1.75 -apple-system,"PingFang SC","Helvetica Neue",sans-serif; }
          .card { max-width:560px; padding:34px 36px; background:var(--surface);
                  border:1px solid var(--line); border-radius:18px;
                  box-shadow:0 40px 90px -50px #000; }
          h1 { font-size:19px; margin:0 0 14px; color:var(--warn); font-weight:650; }
          p { color:var(--dim); margin:0 0 12px; }
          code { font:13px ui-monospace,Menlo,monospace; background:#171b25;
                 border:1px solid var(--line); padding:2px 7px; border-radius:6px; color:#cfe9dd; }
        </style></head><body><div class="card">
          <h1>后台没能启动</h1>
          <p>\(message)</p>
          <p style="margin-top:20px">排查线索：<code>~/.config/yi/logs/agent.log</code></p>
        </div></body></html>
        """
    }

    func buildMenu() {
        let mainMenu = NSMenu()
        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于 yi", action: #selector(showAbout), keyEquivalent: "")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "退出 yi", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appMenuItem.submenu = appMenu
        NSApp.mainMenu = mainMenu
    }

    @objc func showAbout() {
        let alert = NSAlert()
        alert.messageText = "yi"
        alert.informativeText = "一键买机器、装服务端、连上代理。"
        alert.runModal()
    }

    // MARK: - WKUIDelegate（把网页弹窗接到原生）

    func webView(
        _ webView: WKWebView,
        runJavaScriptAlertPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping () -> Void
    ) {
        let alert = NSAlert()
        alert.messageText = "yi"
        alert.informativeText = message
        alert.addButton(withTitle: "好")
        alert.runModal()
        completionHandler()
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptConfirmPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping (Bool) -> Void
    ) {
        let alert = NSAlert()
        alert.messageText = "yi"
        alert.informativeText = message
        alert.addButton(withTitle: "确定")
        alert.addButton(withTitle: "取消")
        completionHandler(alert.runModal() == .alertFirstButtonReturn)
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptTextInputPanelWithPrompt prompt: String,
        defaultText: String?,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping (String?) -> Void
    ) {
        let alert = NSAlert()
        alert.messageText = "yi"
        alert.informativeText = prompt
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 260, height: 24))
        field.stringValue = defaultText ?? ""
        alert.accessoryView = field
        alert.addButton(withTitle: "确定")
        alert.addButton(withTitle: "取消")
        completionHandler(alert.runModal() == .alertFirstButtonReturn ? field.stringValue : nil)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        // 故意**不**停 agent：它负责"机器被回收后自动重建并重连"。
        // 关掉窗口就停止守护的话，用户合上窗口再遇到竞价回收就没人管了。
        // agent 是独立进程，代理内核（mihomo）也是独立进程，两者都不随本窗口退出。
        NSLog("yi: 窗口关闭，agent 继续在后台守护（要停它：pkill -f app/agent.py）")
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()

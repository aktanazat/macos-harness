import AgentScreenCore
import AppKit
import Carbon

/// The menu bar item: peek at the agents' screen, bring the user's windows back (also on
/// Control-Option-Command-B anywhere), send grabbed windows back, and a quiet mark on the icon
/// when an agent window waits on him: a sign-in, a passkey, a robot check.
enum Menu {
  private static var item: NSStatusItem?
  private static var hotKey: EventHotKeyRef?
  private static let target = MenuTarget()
  private static let menu = NSMenu()
  private static var needs: [ServerWindow] = []
  static let peek = PeekWindow()

  static func install() {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    item.menu = menu
    self.item = item
    registerHotKey()
    let t = Timer(timeInterval: 15, repeats: true) { _ in update() }
    t.tolerance = 5
    RunLoop.main.add(t, forMode: .common)
    update()
  }

  /// Rebuilds the menu and the icon from the windows waiting on him and the grabs.
  static func update() {
    let found = windowsOnAgentDisplays().filter { w in
      guard Labels.needsUser(title: w.title), let app = NSRunningApplication(processIdentifier: w.pid) else { return false }
      return WindowGuard.ownedByAgents(app, window: w.id)
    }
    needs = found
    let symbol = found.isEmpty ? "display.2" : "display.trianglebadge.exclamationmark"
    item?.button?.image = NSImage(systemSymbolName: symbol, accessibilityDescription: "Agents' screen")
      ?? NSImage(systemSymbolName: "display.2", accessibilityDescription: "Agents' screen")
    menu.removeAllItems()
    menu.addItem(entry("Peek at the Agents' Screen", #selector(MenuTarget.peek)))
    let back = entry("Bring My Windows Back", #selector(MenuTarget.bringBack))
    back.keyEquivalent = "b"
    back.keyEquivalentModifierMask = [.control, .option, .command]
    menu.addItem(back)
    let grabbed = Grabs.book.grabs.count
    if grabbed > 0 {
      menu.addItem(entry(grabbed == 1 ? "Send Back the Grabbed Window" : "Send Back \(grabbed) Grabbed Windows",
                         #selector(MenuTarget.sendBack)))
    }
    if !found.isEmpty {
      menu.addItem(.separator())
      let header = NSMenuItem(title: "Waiting on You", action: nil, keyEquivalent: "")
      header.isEnabled = false
      menu.addItem(header)
      for (i, w) in found.enumerated() {
        let title = w.title.count > 60 ? String(w.title.prefix(59)) + "…" : w.title
        let m = entry(title, #selector(MenuTarget.grabWaiting(_:)))
        m.tag = i
        menu.addItem(m)
      }
    }
    peek.refreshGrabs()
  }

  fileprivate static func grabWaiting(_ index: Int) {
    guard needs.indices.contains(index) else { return }
    Grabs.grab(needs[index].id, pid: needs[index].pid)
    update()
  }

  private static func entry(_ title: String, _ action: Selector) -> NSMenuItem {
    let m = NSMenuItem(title: title, action: action, keyEquivalent: "")
    m.target = target
    return m
  }

  private static func registerHotKey() {
    var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
    InstallEventHandler(GetApplicationEventTarget(), { _, _, _ in
      DispatchQueue.main.async { Menu.target.bringBack() }
      return noErr
    }, 1, &spec, nil, nil)
    let id = EventHotKeyID(signature: OSType(0x4147_4453), id: 1)
    let status = RegisterEventHotKey(UInt32(kVK_ANSI_B), UInt32(controlKey | optionKey | cmdKey), id,
                                     GetApplicationEventTarget(), 0, &hotKey)
    if status != noErr { log("bring-back shortcut unavailable: another app holds Control-Option-Command-B") }
  }
}

private final class MenuTarget: NSObject {
  @objc func peek() { Menu.peek.show() }

  @objc func bringBack() {
    let n = WindowGuard.bringAllBack()
    log("bring back: moved \(n) window\(n == 1 ? "" : "s")")
  }

  @objc func sendBack() { Grabs.sendAllBack() }

  @objc func grabWaiting(_ sender: NSMenuItem) { Menu.grabWaiting(sender.tag) }
}

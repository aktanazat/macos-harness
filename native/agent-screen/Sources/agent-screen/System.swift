import AgentScreenCore
import AppKit
import ApplicationServices
import Darwin

func log(_ s: String) {
  FileHandle.standardError.write(Data((s + "\n").utf8))
}

// MARK: - Accessibility

@_silgen_name("_AXUIElementGetWindow")
private func _AXUIElementGetWindow(_ element: AXUIElement, _ id: UnsafeMutablePointer<CGWindowID>) -> AXError

/// The window server's id for an Accessibility window.
func windowID(_ w: AXUIElement) -> CGWindowID? {
  var id: CGWindowID = 0
  return _AXUIElementGetWindow(w, &id) == .success && id != 0 ? id : nil
}

func axApp(_ pid: pid_t) -> AXUIElement {
  let el = AXUIElementCreateApplication(pid)
  AXUIElementSetMessagingTimeout(el, 2.0)
  return el
}

func axAttr(_ el: AXUIElement, _ name: String) -> CFTypeRef? {
  var v: CFTypeRef?
  return AXUIElementCopyAttributeValue(el, name as CFString, &v) == .success ? v : nil
}

/// An attribute that holds an element (a focused window), checked before the cast.
func axElement(_ el: AXUIElement, _ name: String) -> AXUIElement? {
  guard let v = axAttr(el, name), CFGetTypeID(v) == AXUIElementGetTypeID() else { return nil }
  return (v as! AXUIElement)
}

func axString(_ el: AXUIElement, _ name: String) -> String? {
  guard let v = axAttr(el, name) else { return nil }
  if let s = v as? String { return s.isEmpty ? nil : s }
  return nil
}

func axFrame(_ el: AXUIElement) -> CGRect? {
  guard let p = axAttr(el, kAXPositionAttribute), let s = axAttr(el, kAXSizeAttribute),
        CFGetTypeID(p) == AXValueGetTypeID(), CFGetTypeID(s) == AXValueGetTypeID() else { return nil }
  var pt = CGPoint.zero, sz = CGSize.zero
  AXValueGetValue(p as! AXValue, .cgPoint, &pt)
  AXValueGetValue(s as! AXValue, .cgSize, &sz)
  return CGRect(origin: pt, size: sz)
}

func axWindows(_ pid: pid_t) -> [AXUIElement] {
  (axAttr(axApp(pid), kAXWindowsAttribute) as? [AXUIElement]) ?? []
}

func axWindow(pid: pid_t, id: CGWindowID) -> AXUIElement? {
  axWindows(pid).first { windowID($0) == id }
}

/// Sets a window's frame; position again after size, since some apps clamp the position
/// against the old size.
func setFrame(_ w: AXUIElement, _ frame: CGRect) {
  var o = frame.origin, s = frame.size
  if let pv = AXValueCreate(.cgPoint, &o) { AXUIElementSetAttributeValue(w, kAXPositionAttribute as CFString, pv) }
  if let sv = AXValueCreate(.cgSize, &s) { AXUIElementSetAttributeValue(w, kAXSizeAttribute as CFString, sv) }
  if let pv = AXValueCreate(.cgPoint, &o) { AXUIElementSetAttributeValue(w, kAXPositionAttribute as CFString, pv) }
}

// MARK: - Displays

/// A display agent-display hosts: `~/.agent-display/displays/<name>.json`, counted while its
/// host process lives.
struct AgentDisplay: Decodable {
  let name: String
  let pid: Int32
  let displayID: UInt32

  static var stateDir: URL {
    FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".agent-display", isDirectory: true)
  }

  static func all() -> [AgentDisplay] {
    let dir = stateDir.appendingPathComponent("displays", isDirectory: true)
    let files = (try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)) ?? []
    return files.filter { $0.pathExtension == "json" }.compactMap { f in
      guard let data = try? Data(contentsOf: f),
            let s = try? JSONDecoder().decode(AgentDisplay.self, from: data), kill(s.pid, 0) == 0 else { return nil }
      return s
    }.sorted { $0.name < $1.name }
  }
}

func activeDisplays() -> [CGDirectDisplayID] {
  var count: UInt32 = 0
  CGGetActiveDisplayList(0, nil, &count)
  var ids = [CGDirectDisplayID](repeating: 0, count: Int(count))
  CGGetActiveDisplayList(count, &ids, &count)
  return Array(ids.prefix(Int(count)))
}

/// Agent displays that are on now.
func agentDisplays() -> [CGDirectDisplayID] {
  let active = activeDisplays()
  return AgentDisplay.all().map(\.displayID).filter(active.contains)
}

/// The agents' screen: the agent display named "Agents", when it is on. Other agent displays
/// (test displays, ones under agent-display's default name) are never it.
func agentsDisplay() -> CGDirectDisplayID? {
  guard let id = AgentDisplay.all().first(where: { $0.name == "Agents" })?.displayID else { return nil }
  return activeDisplays().contains(id) ? id : nil
}

func displayFor(point p: CGPoint) -> CGDirectDisplayID? {
  activeDisplays().first { CGDisplayBounds($0).contains(p) }
}

func agentDisplay(containing r: CGRect) -> CGDirectDisplayID? {
  agentDisplays().first { CGDisplayBounds($0).contains(CGPoint(x: r.midX, y: r.midY)) }
}

/// The display under the cursor when it is one of his, else the main display, else any of his.
func userDisplay() -> CGDirectDisplayID? {
  let agents = agentDisplays()
  let real = activeDisplays().filter { !agents.contains($0) }
  let cursor = CGEvent(source: nil)?.location ?? .zero
  if let d = displayFor(point: cursor), real.contains(d) { return d }
  let main = CGMainDisplayID()
  return real.contains(main) ? main : real.first
}

/// The display's bounds minus the menu bar and Dock, in global top-left coordinates.
func usableArea(_ id: CGDirectDisplayID) -> CGRect {
  let bounds = CGDisplayBounds(id)
  let key = NSDeviceDescriptionKey("NSScreenNumber")
  guard let screen = NSScreen.screens.first(where: { ($0.deviceDescription[key] as? NSNumber)?.uint32Value == id }) else {
    return CGRect(x: bounds.minX, y: bounds.minY + 25, width: bounds.width, height: bounds.height - 25)
  }
  let f = screen.frame, v = screen.visibleFrame
  return CGRect(x: bounds.minX + (v.minX - f.minX), y: bounds.minY + (f.maxY - v.maxY), width: v.width, height: v.height)
}

/// One window as the window server lists it.
struct ServerWindow {
  let id: CGWindowID
  let pid: pid_t
  let layer: Int
  let frame: CGRect
  let title: String
  let owner: String
  let onScreen: Bool
}

/// The window server's list, front to back, read into `ServerWindow`s at this one boundary.
func serverWindows(_ options: CGWindowListOption, relativeTo window: CGWindowID = kCGNullWindowID) -> [ServerWindow] {
  let info = CGWindowListCopyWindowInfo(options, window) as? [NSDictionary] ?? []
  return info.compactMap { w in
    guard let id = (w[kCGWindowNumber] as? NSNumber)?.uint32Value,
          let pid = (w[kCGWindowOwnerPID] as? NSNumber)?.int32Value,
          let bounds = w[kCGWindowBounds] as? NSDictionary,
          let frame = CGRect(dictionaryRepresentation: bounds as CFDictionary) else { return nil }
    return ServerWindow(id: id, pid: pid, layer: (w[kCGWindowLayer] as? NSNumber)?.intValue ?? 0, frame: frame,
                        title: w[kCGWindowName] as? String ?? "", owner: w[kCGWindowOwnerName] as? String ?? "",
                        onScreen: (w[kCGWindowIsOnscreen] as? NSNumber)?.boolValue ?? false)
  }
}

/// Ordinary windows (layer 0) whose centre is on an agent display, front to back.
func windowsOnAgentDisplays(onScreenOnly: Bool = false) -> [ServerWindow] {
  let agents = agentDisplays().map(CGDisplayBounds)
  guard !agents.isEmpty else { return [] }
  let options: CGWindowListOption = onScreenOnly ? [.optionOnScreenOnly, .excludeDesktopElements] : [.optionAll, .excludeDesktopElements]
  return serverWindows(options).filter { w in
    w.layer == 0 && agents.contains { $0.contains(CGPoint(x: w.frame.midX, y: w.frame.midY)) }
  }
}

func windowTitle(_ id: CGWindowID) -> String? {
  serverWindows([.optionIncludingWindow], relativeTo: id).first?.title
}

// MARK: - Processes

struct ProcessFacts {
  let name: String
  let parent: pid_t
}

func processFacts(_ pid: pid_t) -> ProcessFacts? {
  var info = proc_bsdinfo()
  let size = Int32(MemoryLayout<proc_bsdinfo>.size)
  guard pid > 0, proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, size) == size else { return nil }
  let name = withUnsafeBytes(of: info.pbi_comm) { String(decoding: $0.prefix { $0 != 0 }, as: UTF8.self) }
  return ProcessFacts(name: name, parent: pid_t(info.pbi_ppid))
}

func processCwd(_ pid: pid_t) -> String? {
  var info = proc_vnodepathinfo()
  let size = Int32(MemoryLayout<proc_vnodepathinfo>.size)
  guard proc_pidinfo(pid, PROC_PIDVNODEPATHINFO, 0, &info, size) == size else { return nil }
  return withUnsafeBytes(of: info.pvi_cdir.vip_path) { String(decoding: $0.prefix { $0 != 0 }, as: UTF8.self) }
}

/// Process names of agent harnesses whose lifetime bounds an agent's session.
let agentHosts: Set<String> = ["omp", "claude", "codex", "pi", "akyl"]

/// The nearest agent harness at or above `pid`, or nil.
func agentHost(of pid: pid_t) -> (pid: pid_t, name: String)? {
  var p = pid
  while p > 1, let facts = processFacts(p) {
    if agentHosts.contains(facts.name) { return (p, facts.name) }
    p = facts.parent
  }
  return nil
}

// MARK: - Claims

/// The claims file agent-display keeps, shared through `ClaimsStore`.
enum Claims {
  private static var store: ClaimsStore { ClaimsStore(directory: AgentDisplay.stateDir) }

  /// Live claims; a claim ends when its process exits.
  static func all() -> [Claim] { store.all() }

  static func owns(_ pid: pid_t, window: CGWindowID?) -> Bool {
    guard let window else { return false }
    return all().first { $0.pid == pid }?.windows.contains(window) == true
  }

  static func add(_ pid: pid_t, windows: Set<CGWindowID>, wholeApp: Bool, launched: Bool) {
    guard let start = ProcessStart.of(pid) else { return }
    store.update { claims in
      claims = ClaimsFile.adding(claims, pid: pid, start: start, windows: windows, wholeApp: wholeApp,
                                 launched: launched)
    }
  }

  /// Removes `claim`, or marks it quitting, when no other writer changed it since it was read.
  @discardableResult
  static func end(_ claim: Claim, quitting: Bool) -> Bool {
    store.update { claims in
      guard let ended = ClaimsFile.ending(claims, claim, quitting: quitting) else { return false }
      claims = ended
      return true
    } == true
  }
}

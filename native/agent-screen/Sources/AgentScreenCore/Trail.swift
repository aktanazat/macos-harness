import Foundation

/// One datagram macos-harness sends the companion (`activity.py`): where an agent's arrow is,
/// or one action it took. Never carries typed text or values; see `activity.py`.
public struct ActivityEvent: Decodable, Equatable, Sendable {
  public struct Target: Decodable, Equatable, Sendable {
    public let role: String?
    public let title: String?
    public let description: String?

    public init(role: String?, title: String?, description: String?) {
      self.role = role
      self.title = title
      self.description = description
    }
  }

  public let v: Int
  public let kind: String
  /// The harness process that sent it.
  public let pid: pid_t
  /// Its `--agent-id`, which names and colours its arrow.
  public let agent: String?
  /// Seconds since 1970 when it happened.
  public let t: Double
  // pointer
  public let event: String?
  public let x: Double?
  public let y: Double?
  public let duration: Double?
  // action
  public let op: String?
  public let source: String?
  public let appPid: pid_t?
  /// The app the caller named, when the harness found no pid for it.
  public let app: String?
  public let windowID: UInt32?
  public let target: Target?
  public let outcome: String?
  public let durationS: Double?
  public let key: String?

  enum CodingKeys: String, CodingKey {
    case v, kind, pid, agent, t, event, x, y, duration, op, source, target, outcome, key, app
    case appPid = "app_pid"
    case windowID = "window_id"
    case durationS = "duration_s"
  }

  public static func decode(_ data: Data) -> ActivityEvent? {
    guard let e = try? JSONDecoder().decode(ActivityEvent.self, from: data), e.v == 1 else { return nil }
    return e
  }
}

/// One line of the trail: what one agent did, newest first.
public struct TrailEntry: Equatable, Sendable {
  public let time: Date
  public let agent: String
  public let senderPid: pid_t
  public let appPid: pid_t?
  public let fromReceipt: Bool
  public let text: String
  public var count: Int

  public init(time: Date, agent: String, senderPid: pid_t, appPid: pid_t?, fromReceipt: Bool, text: String,
              count: Int) {
    self.time = time
    self.agent = agent
    self.senderPid = senderPid
    self.appPid = appPid
    self.fromReceipt = fromReceipt
    self.text = text
    self.count = count
  }
}

public struct Trail: Sendable {
  public static let capacity = 400
  /// Repeats of the same line from the same agent this close together count up one line.
  public static let repeatWindow: TimeInterval = 10

  public private(set) var entries: [TrailEntry] = []

  public init() {}

  /// Adds `entry`. A `mac.do` receipt names the control it acted on and replaces the plain
  /// clicks and keys the same process sent while it ran, which say less.
  public mutating func add(_ entry: TrailEntry, receiptDuration: TimeInterval = 0) {
    if entry.fromReceipt {
      let start = entry.time.addingTimeInterval(-receiptDuration - 0.05)
      let end = entry.time.addingTimeInterval(0.05)
      entries.removeAll {
        !$0.fromReceipt && $0.senderPid == entry.senderPid && $0.time >= start && $0.time <= end
      }
    }
    if let top = entries.first, top.agent == entry.agent, top.text == entry.text,
       entry.time.timeIntervalSince(top.time) < Self.repeatWindow {
      entries[0] = TrailEntry(time: entry.time, agent: top.agent, senderPid: entry.senderPid,
                              appPid: entry.appPid, fromReceipt: entry.fromReceipt, text: top.text,
                              count: top.count + 1)
      return
    }
    entries.insert(entry, at: 0)
    if entries.count > Self.capacity { entries.removeLast(entries.count - Self.capacity) }
  }
}

public enum TrailWording {
  /// Lookups that change nothing; the trail is for what agents did.
  public static let quiet: Set<String> = ["expect", "expect_any", "menu_search", "recall", "run"]

  /// "clicked “Save” in TextEdit": the verb, the control, and where.
  public static func phrase(op: String, target: ActivityEvent.Target?, app: String?, window: String?,
                            key: String?, failed: Bool) -> String {
    let control = target.flatMap(label)
    let place: String = {
      guard let app else { return "" }
      if control == nil, let window, !window.isEmpty, window != app {
        return " in \(app) — \(clip(window, 60))"
      }
      return " in \(app)"
    }()
    let object = control.map { " \($0)" } ?? ""
    let action: String
    switch op {
    case "click": action = "clicked\(object)"
    case "press": action = "pressed\(object)"
    case "type", "fill": action = control.map { "typed into \($0)" } ?? "typed"
    case "paste": action = control.map { "pasted into \($0)" } ?? "pasted"
    case "set": action = "set\(object)"
    case "toggle": action = "toggled\(object)"
    case "key": action = "pressed \(key ?? "a key")"
    case "scroll": action = "scrolled"
    case "drag": action = "dragged"
    case "open_files": action = "opened files"
    case "reveal": action = "showed\(object)"
    case "window": action = "changed a window"
    default: action = op.replacingOccurrences(of: "_", with: " ") + object
    }
    return (failed ? "tried: " : "") + action + place
  }

  private static func label(_ t: ActivityEvent.Target) -> String? {
    for s in [t.title, t.description] {
      if let s = s?.trimmingCharacters(in: .whitespacesAndNewlines), !s.isEmpty {
        return "“\(clip(s, 48))”"
      }
    }
    switch t.role {
    case "AXTextField", "AXSearchField", "AXComboBox": return "a text field"
    case "AXSecureTextField": return "a password field"
    case "AXTextArea": return "a text area"
    case "AXButton": return "a button"
    case "AXCheckBox": return "a checkbox"
    case "AXMenuItem": return "a menu item"
    case nil: return nil
    default: return "a control"
    }
  }

  private static func clip(_ s: String, _ n: Int) -> String {
    s.count <= n ? s : String(s.prefix(n - 1)) + "…"
  }
}

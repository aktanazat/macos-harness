import Foundation

/// When to go easy on the battery, and which agent apps to pause then.
public enum Power {
  /// An agent app with no window activity this long is paused while saving power.
  public static let idleBeforePause: TimeInterval = 600

  public static func saving(onBattery: Bool, thermal: ProcessInfo.ThermalState) -> Bool {
    onBattery || thermal == .serious || thermal == .critical
  }

  /// Seconds between peek pictures.
  public static func peekInterval(saving: Bool) -> TimeInterval { saving ? 2 : 0.5 }

  public struct App: Equatable, Sendable {
    public let pid: pid_t
    /// Started by an agent; the user's own apps are never paused.
    public let launched: Bool
    public let frontmost: Bool
    /// A window of it is on his screen through the peek.
    public let grabbed: Bool
    public let lastActivity: Date

    public init(pid: pid_t, launched: Bool, frontmost: Bool, grabbed: Bool, lastActivity: Date) {
      self.pid = pid
      self.launched = launched
      self.frontmost = frontmost
      self.grabbed = grabbed
      self.lastActivity = lastActivity
    }
  }

  public static func toPause(_ apps: [App], now: Date) -> [pid_t] {
    apps.filter {
      $0.launched && !$0.frontmost && !$0.grabbed && now.timeIntervalSince($0.lastActivity) >= idleBeforePause
    }.map(\.pid)
  }
}

/// When each app's windows last changed (opened, closed, moved, resized, retitled).
public struct WindowActivity: Sendable {
  private var seen: [pid_t: (signature: Int, at: Date)] = [:]

  public init() {}

  /// Records `signature`, a digest of the app's windows now; a change counts as activity.
  public mutating func observe(_ pid: pid_t, signature: Int, at now: Date) {
    if let prior = seen[pid], prior.signature == signature { return }
    seen[pid] = (signature, now)
  }

  public mutating func touch(_ pid: pid_t, at now: Date) {
    seen[pid] = (seen[pid]?.signature ?? 0, now)
  }

  public func last(_ pid: pid_t) -> Date? { seen[pid]?.at }

  public mutating func keep(_ pids: Set<pid_t>) {
    seen = seen.filter { pids.contains($0.key) }
  }
}

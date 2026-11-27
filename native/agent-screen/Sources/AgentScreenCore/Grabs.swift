import CoreGraphics
import Foundation

/// A window the user took from the agents' screen through the peek, and where it came from.
public struct Grab: Equatable, Sendable {
  public let windowID: UInt32
  public let pid: pid_t
  /// Its frame on the agent display when he took it, in global top-left coordinates.
  public let home: CGRect
  /// A window of a browser the browser harness drives, which goes back sooner (see
  /// `GrabBook.safariIdleLimit`).
  public let safari: Bool
  public let grabbedAt: Date
  public fileprivate(set) var lastUsed: Date
}

/// The windows the user grabbed and when each goes back.
public struct GrabBook: Sendable {
  /// A grabbed window he has not used for this long goes back.
  public static let idleLimit: TimeInterval = 120
  /// The browser harness checks its agent windows every 60 s and takes one found off the
  /// agents' display as moved there by the user, for good; one he left alone goes back before that.
  public static let safariIdleLimit: TimeInterval = 45

  public private(set) var grabs: [Grab] = []

  public init() {}

  /// Records a grab. Grabbing a window already grabbed keeps its first home, which is where it
  /// lived on the agents' screen.
  public mutating func grab(windowID: UInt32, pid: pid_t, home: CGRect, safari: Bool, at now: Date) {
    if let i = grabs.firstIndex(where: { $0.windowID == windowID }) {
      grabs[i].lastUsed = now
      return
    }
    grabs.append(Grab(windowID: windowID, pid: pid, home: home, safari: safari, grabbedAt: now,
                      lastUsed: now))
  }

  public func contains(_ windowID: UInt32) -> Bool { grabs.contains { $0.windowID == windowID } }

  /// He used the window (it had focus, or his pointer was over it).
  public mutating func used(_ windowID: UInt32, at now: Date) {
    guard let i = grabs.firstIndex(where: { $0.windowID == windowID }) else { return }
    grabs[i].lastUsed = max(grabs[i].lastUsed, now)
  }

  /// Grabs left unused past their limit, which go back now.
  public func due(at now: Date) -> [Grab] {
    grabs.filter { now.timeIntervalSince($0.lastUsed) >= ($0.safari ? Self.safariIdleLimit : Self.idleLimit) }
  }

  /// When the next grab falls due, so a timer runs only while one is pending.
  public func nextDue() -> Date? {
    grabs.map { $0.lastUsed.addingTimeInterval($0.safari ? Self.safariIdleLimit : Self.idleLimit) }.min()
  }

  @discardableResult
  public mutating func release(_ windowID: UInt32) -> Grab? {
    guard let i = grabs.firstIndex(where: { $0.windowID == windowID }) else { return nil }
    return grabs.remove(at: i)
  }

  /// Drops grabs whose windows closed.
  public mutating func keep(alive: Set<UInt32>) {
    grabs.removeAll { !alive.contains($0.windowID) }
  }
}

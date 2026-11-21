import Darwin
import Foundation

/// Who stands behind a claim.
public enum ClaimState: Equatable, Sendable {
  /// The agent sessions that asked for the app.
  case active(owners: Set<pid_t>)
  /// Every owner ended and the companion is quitting the app; an agent that wants the app again
  /// starts a fresh copy instead of joining this one.
  case quitting
}

/// Windows agents placed on an agent display with `agent-display launch` or `move`; the guard
/// leaves exactly these alone. With `wholeApp`, the app is the agents': windows it opens later
/// while the user is not in it join the claim. Once all owners have exited, an app an agent
/// started (`launched`) is quit, unless the user has windows of his own in it.
public struct Claim: Equatable, Sendable {
  public let pid: pid_t
  /// When the claimed process started (`ProcessStart.of`), so a later process given the same
  /// pid is not taken for it.
  public let start: UInt64
  public let state: ClaimState
  public let launched: Bool
  public let wholeApp: Bool
  public let windows: Set<UInt32>

  public init(pid: pid_t, start: UInt64, state: ClaimState, launched: Bool, wholeApp: Bool,
              windows: Set<UInt32>) {
    self.pid = pid
    self.start = start
    self.state = state
    self.launched = launched
    self.wholeApp = wholeApp
    self.windows = windows
  }

  public var owners: Set<pid_t> {
    if case .active(let owners) = state { return owners }
    return []
  }
}

/// The claims file agent-display and the companion share, `~/.agent-display/claims`: one line
/// per app, `pid start owners launched wholeApp windows`, where owners is a comma list, `-` for
/// none, or `quitting`, and windows is a comma list or `-`. A line in the earlier format, without
/// `start`, belongs to whatever process holds its pid now (`startOf`) and is written back in
/// this format.
public enum ClaimsFile {
  public static func parse(_ text: String, startOf: (pid_t) -> UInt64?) -> [Claim] {
    text.split(whereSeparator: \.isNewline).compactMap { line in
      var f = line.split(separator: " ")
      guard let pid = pid_t(f[0]), pid > 0 else { return nil }
      if f.count == 5, let start = startOf(pid) { f.insert(Substring(String(start)), at: 1) }
      guard f.count == 6, let start = UInt64(f[1]) else { return nil }
      let state: ClaimState = f[2] == "quitting" ? .quitting : .active(owners: ids(f[2]))
      return Claim(pid: pid, start: start, state: state, launched: f[3] == "1", wholeApp: f[4] == "1",
                   windows: ids(f[5]))
    }
  }

  public static func format(_ claims: [Claim]) -> String {
    claims.map { c in
      let state: String
      switch c.state {
      case .active(let owners): state = list(owners)
      case .quitting: state = "quitting"
      }
      return "\(c.pid) \(c.start) \(state) \(c.launched ? 1 : 0) \(c.wholeApp ? 1 : 0) \(list(c.windows))"
    }.joined(separator: "\n")
  }

  /// `claims` with `windows` added to the claim on the process `pid` started at `start`, or a
  /// new claim for it. An app being quit takes no more windows.
  public static func adding(_ claims: [Claim], pid: pid_t, start: UInt64, windows: Set<UInt32>,
                            wholeApp: Bool, launched: Bool) -> [Claim] {
    let prior = claims.first { $0.pid == pid && $0.start == start }
    if prior?.state == .quitting { return claims }
    var out = claims.filter { $0.pid != pid }
    out.append(Claim(pid: pid, start: start, state: prior?.state ?? .active(owners: []),
                     launched: launched || prior?.launched == true,
                     wholeApp: wholeApp || prior?.wholeApp == true,
                     windows: windows.union(prior?.windows ?? [])))
    return out
  }

  /// `claims` without `claim`, or with it marked quitting, when it is still exactly as it was
  /// read; nil when another writer changed it since.
  public static func ending(_ claims: [Claim], _ claim: Claim, quitting: Bool) -> [Claim]? {
    guard let i = claims.firstIndex(of: claim) else { return nil }
    var out = claims
    if quitting {
      out[i] = Claim(pid: claim.pid, start: claim.start, state: .quitting, launched: claim.launched,
                     wholeApp: claim.wholeApp, windows: claim.windows)
    } else {
      out.remove(at: i)
    }
    return out
  }

  /// What the 30 s sweep does with each live claim: quit the apps whose owners all ended, and
  /// release one it already asked to quit that is still running (a save sheet kept it open),
  /// which is the user's from then on.
  public static func sweep(_ claims: [Claim], ownerAlive: (pid_t) -> Bool) -> [SweepStep] {
    claims.compactMap { c in
      switch c.state {
      case .quitting: return .release(c)
      case .active(let owners) where !owners.isEmpty && !owners.contains(where: ownerAlive): return .quit(c)
      case .active: return nil
      }
    }
  }

  private static func ids<T: FixedWidthInteger>(_ s: Substring) -> Set<T> {
    Set(s.split(separator: ",").compactMap { T(String($0)) })
  }

  private static func list<T: FixedWidthInteger>(_ s: Set<T>) -> String {
    s.isEmpty ? "-" : s.sorted().map(String.init).joined(separator: ",")
  }
}

public enum SweepStep: Equatable, Sendable {
  case quit(Claim)
  case release(Claim)
}

public enum ProcessStart {
  /// When `pid`'s process started, in microseconds since 1970, or nil when there is none.
  public static func of(_ pid: pid_t) -> UInt64? {
    var info = proc_bsdinfo()
    let size = Int32(MemoryLayout<proc_bsdinfo>.size)
    guard pid > 0, proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, size) == size else { return nil }
    return info.pbi_start_tvsec * 1_000_000 + info.pbi_start_tvusec
  }
}

/// The claims file in `directory`. Every change is a read-modify-write under an exclusive
/// flock on `claims.lock`, which agent-display takes too, so no writer drops another's change.
public struct ClaimsStore: Sendable {
  public let directory: URL

  public init(directory: URL) {
    self.directory = directory
  }

  /// Claims whose process is still the one claimed.
  public func all() -> [Claim] {
    let text = (try? String(contentsOf: directory.appendingPathComponent("claims"), encoding: .utf8)) ?? ""
    return ClaimsFile.parse(text, startOf: ProcessStart.of).filter { ProcessStart.of($0.pid) == $0.start }
  }

  /// Saves what `change` makes of the live claims and answers its result; claims of ended
  /// processes are dropped on the way. Nil, with nothing changed, when the lock cannot be opened.
  @discardableResult
  public func update<T>(_ change: (inout [Claim]) -> T) -> T? {
    let fd = open(directory.appendingPathComponent("claims.lock").path, O_RDWR | O_CREAT | O_CLOEXEC, 0o600)
    guard fd >= 0 else { return nil }
    defer { close(fd) }
    flock(fd, LOCK_EX)
    var claims = all()
    let result = change(&claims)
    try? ClaimsFile.format(claims).write(to: directory.appendingPathComponent("claims"), atomically: true,
                                         encoding: .utf8)
    return result
  }
}

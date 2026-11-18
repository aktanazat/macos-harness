import Foundation

/// Windows agents placed on an agent display with `agent-display launch` or `move`; the guard
/// leaves exactly these alone. With `wholeApp`, the app is the agents': windows it opens later
/// while the user is not in it join the claim. `owners` are the agent sessions that asked; once
/// all have exited, an app an agent started (`launched`) is quit, unless the user has windows
/// of his own in it.
public struct Claim: Equatable, Sendable {
  public let pid: pid_t
  public let owners: Set<pid_t>
  public let launched: Bool
  public let wholeApp: Bool
  public let windows: Set<UInt32>

  public init(pid: pid_t, owners: Set<pid_t>, launched: Bool, wholeApp: Bool, windows: Set<UInt32>) {
    self.pid = pid
    self.owners = owners
    self.launched = launched
    self.wholeApp = wholeApp
    self.windows = windows
  }
}

/// The claims file agent-display writes, `~/.agent-display/claims`: one line per app,
/// `pid owners launched wholeApp windows`, where owners and windows are comma lists or `-`.
public enum ClaimsFile {
  public static func parse(_ text: String) -> [Claim] {
    text.split(whereSeparator: \.isNewline).compactMap { line in
      let f = line.split(separator: " ")
      guard f.count == 5, let pid = pid_t(f[0]), pid > 0 else { return nil }
      return Claim(pid: pid, owners: ids(f[1]), launched: f[2] == "1", wholeApp: f[3] == "1",
                   windows: ids(f[4]))
    }
  }

  public static func format(_ claims: [Claim]) -> String {
    claims.map { c in
      "\(c.pid) \(list(c.owners)) \(c.launched ? 1 : 0) \(c.wholeApp ? 1 : 0) \(list(c.windows))"
    }.joined(separator: "\n")
  }

  /// `claims` with `windows` added to `pid`'s claim, or a new claim for it.
  public static func adding(_ claims: [Claim], pid: pid_t, windows: Set<UInt32>, wholeApp: Bool,
                            launched: Bool) -> [Claim] {
    var out = claims
    let prior = out.first { $0.pid == pid }
    out.removeAll { $0.pid == pid }
    out.append(Claim(pid: pid, owners: prior?.owners ?? [], launched: launched || prior?.launched == true,
                     wholeApp: wholeApp || prior?.wholeApp == true,
                     windows: windows.union(prior?.windows ?? [])))
    return out
  }

  private static func ids<T: FixedWidthInteger>(_ s: Substring) -> Set<T> {
    Set(s.split(separator: ",").compactMap { T(String($0)) })
  }

  private static func list<T: FixedWidthInteger>(_ s: Set<T>) -> String {
    s.isEmpty ? "-" : s.sorted().map(String.init).joined(separator: ",")
  }
}

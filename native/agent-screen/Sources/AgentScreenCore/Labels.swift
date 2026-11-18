import CryptoKit
import Foundation

public enum Labels {
  /// Who a Safari helper window belongs to, from its title: "agent <pid>, window <n> — page"
  /// names the agent's session pid; "group — page" names the agent's group.
  public static func safariOwner(title: String) -> (agentPid: pid_t?, group: String?) {
    let head = title.components(separatedBy: " — ").first ?? title
    let words = head.split(separator: " ")
    if words.count >= 2, words[0] == "agent", let pid = pid_t(words[1].trimmingCharacters(in: [","])) {
      return (pid, nil)
    }
    let group = head.trimmingCharacters(in: .whitespaces)
    return (nil, group.isEmpty ? nil : group)
  }

  /// A window the user himself has to act in: a sign-in, a passkey, a robot check.
  public static func needsUser(title: String) -> Bool {
    let t = title.lowercased()
    return needles.contains { t.contains($0) }
  }

  private static let needles = [
    "sign in", "sign-in", "signin", "log in", "login", "authentication", "authenticate",
    "verify", "verification", "captcha", "robot", "two-factor", "2fa", "passkey",
    "security check", "just a moment", "attention required",
  ]

  /// A session's name when its agent gave none: the harness and the folder it works in,
  /// "omp · macos-harness", else the harness and its pid.
  public static func session(host: String, pid: pid_t, cwd: String?) -> String {
    guard let cwd, !cwd.isEmpty, cwd != "/" else { return "\(host) \(pid)" }
    let folder = (cwd as NSString).lastPathComponent
    return folder.isEmpty ? "\(host) \(pid)" : "\(host) · \(folder)"
  }

  /// The colour of an agent's arrow, as `PointerIdentity.color` in macos-harness's pointer.py
  /// computes it, so the peek and the live arrow agree: the hue from the first three bytes of
  /// SHA-256 of the name, saturation 0.72, value 0.58.
  public static func color(of name: String) -> (red: Double, green: Double, blue: Double) {
    let digest = Array(SHA256.hash(data: Data(name.utf8)))
    let hue = Double(UInt32(digest[0]) << 16 | UInt32(digest[1]) << 8 | UInt32(digest[2])) / 0x1000000
    return hsv(hue, 0.72, 0.58)
  }

  /// Python's colorsys.hsv_to_rgb.
  private static func hsv(_ h: Double, _ s: Double, _ v: Double) -> (Double, Double, Double) {
    let i = Int(h * 6)
    let f = h * 6 - Double(i)
    let p = v * (1 - s), q = v * (1 - s * f), t = v * (1 - s * (1 - f))
    switch i % 6 {
    case 0: return (v, t, p)
    case 1: return (q, v, p)
    case 2: return (p, v, t)
    case 3: return (p, q, v)
    case 4: return (t, p, v)
    default: return (v, p, q)
    }
  }
}

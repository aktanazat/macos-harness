import Foundation

/// Whose the app in front is, for the guard that moves its windows off agent displays.
public enum Frontmost {
  /// He brought the app forward when he pressed a key or used the pointer this recently.
  public static let personWindow: TimeInterval = 2
  /// An agent brought the app forward or is driving it when it acted on it this recently.
  public static let agentWindow: TimeInterval = 10

  /// Any app no agent claimed is his. A claimed app is his only right after his own input and
  /// with no agent acting on it just now: it may come forward by itself (an agent's build
  /// activating as it starts), or because an agent brought it forward in a pause of his typing.
  public static func isHis(appClaimed: Bool, sincePerson: TimeInterval, sinceAgentActed: TimeInterval?) -> Bool {
    !appClaimed || (sincePerson < personWindow && (sinceAgentActed ?? .infinity) >= agentWindow)
  }
}

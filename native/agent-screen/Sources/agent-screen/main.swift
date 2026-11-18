import AppKit
import ApplicationServices

let usage = """
  agent-screen: the menu bar companion for the agents' hidden screen.

  USAGE
    agent-screen run          Stay running (a launch agent runs this): keep your windows off
                              agent displays, keep those displays in the corner, peek at them,
                              trace what agents do, and pause idle agent apps on battery.
    agent-screen bring-back   Move every window of your own apps off agent displays.
  """

private var signalSources: [DispatchSourceSignal] = []

private func requireAccessibility(waiting: Bool) {
  let prompt = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
  guard !AXIsProcessTrustedWithOptions([prompt: waiting] as CFDictionary) else { return }
  guard waiting else {
    print(#"{"error": "Accessibility permission missing for agent-screen"}"#)
    exit(1)
  }
  log("waiting: grant Accessibility to \(CommandLine.arguments[0]) in System Settings > Privacy & Security > Accessibility")
  while !AXIsProcessTrusted() { sleep(5) }
}

/// One companion at a time: a second would steal the first's activity socket.
private func holdLock() {
  let dir = (Activity.socketPath as NSString).deletingLastPathComponent
  try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
  let fd = open((dir as NSString).appendingPathComponent("agent-screen.lock"), O_RDWR | O_CREAT, 0o600)
  guard fd >= 0, flock(fd, LOCK_EX | LOCK_NB) == 0 else {
    log("another agent-screen is already running")
    exit(0)
  }
}

private func run() -> Never {
  _ = NSApplication.shared
  NSApplication.shared.setActivationPolicy(.accessory)
  holdLock()
  requireAccessibility(waiting: true)
  for sig in [SIGTERM, SIGINT, SIGHUP] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler {
      Battery.resumeAll()
      Grabs.sendAllBack()
      Activity.stop()
      exit(0)
    }
    src.resume()
    signalSources.append(src)
  }
  Activity.onChange = { Menu.peek.activityChanged() }
  Grabs.onChange = { Menu.update() }
  Battery.onChange = { Menu.peek.restartCapture() }
  WindowGuard.start()
  Menu.install()
  AgentApps.start()
  Activity.start()
  Battery.start()
  log("agent-screen on")
  NSApplication.shared.run()
  exit(0)
}

switch CommandLine.arguments.dropFirst().first {
case "run":
  run()
case "bring-back":
  _ = NSApplication.shared
  requireAccessibility(waiting: false)
  print(#"{"moved": \#(WindowGuard.bringAllBack())}"#)
case "help", "--help", "-h", nil:
  print(usage)
default:
  print(usage)
  exit(2)
}

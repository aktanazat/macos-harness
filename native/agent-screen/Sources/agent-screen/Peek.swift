import AgentScreenCore
import AppKit
import ScreenCaptureKit

/// A floating live view of the agents' screen: each window labelled with the agent using it,
/// each agent's arrow, the trail of what agents did beside it, and a click on a window brings
/// that window to the user's screen. Captures only while open.
final class PeekWindow: NSObject, NSWindowDelegate {
  private var panel: NSPanel?
  private let picture = PeekView()
  private let trailView = NSTextView()
  private let sendBack = NSButton(title: "Send Back", target: nil, action: nil)
  private let hint = NSTextField(labelWithString: "Click a window to bring it to your screen")
  private var captureTimer: Timer?
  private var motionTimer: Timer?
  private var capturing = false
  private var trailDirty = false

  var isOpen: Bool { panel?.isVisible == true }

  func show() {
    let panel = self.panel ?? makePanel()
    self.panel = panel
    panel.orderFrontRegardless()
    if !CGPreflightScreenCaptureAccess() {
      CGRequestScreenCaptureAccess()
      panel.title = "Allow Screen Recording for agent-screen, then reopen"
    }
    restartCapture()
    refreshTrail()
    refreshGrabs()
  }

  func windowWillClose(_ notification: Notification) {
    captureTimer?.invalidate()
    captureTimer = nil
    motionTimer?.invalidate()
    motionTimer = nil
  }

  /// The capture pace follows the battery: slower while saving power.
  func restartCapture() {
    captureTimer?.invalidate()
    captureTimer = nil
    guard isOpen, CGPreflightScreenCaptureAccess() else { return }
    refresh()
    let t = Timer(timeInterval: Power.peekInterval(saving: Battery.saving), repeats: true) { [weak self] _ in self?.refresh() }
    t.tolerance = Power.peekInterval(saving: Battery.saving) / 4
    RunLoop.main.add(t, forMode: .common)
    captureTimer = t
  }

  /// New activity: redraw arrows (smoothly while one moves) and, at most four times a
  /// second, the trail.
  func activityChanged() {
    guard isOpen else { return }
    picture.needsDisplay = true
    if motionTimer == nil, Activity.prunePointers(at: Date()) {
      let t = Timer(timeInterval: 1.0 / 30, repeats: true) { [weak self] timer in
        guard let self else { return timer.invalidate() }
        self.picture.needsDisplay = true
        if !Activity.prunePointers(at: Date()) {
          timer.invalidate()
          self.motionTimer = nil
        }
      }
      RunLoop.main.add(t, forMode: .common)
      motionTimer = t
    }
    guard !trailDirty else { return }
    trailDirty = true
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { [weak self] in
      self?.trailDirty = false
      self?.refreshTrail()
    }
  }

  func refreshGrabs() {
    let n = Grabs.book.grabs.count
    sendBack.isHidden = n == 0
    sendBack.title = n == 1 ? "Send Back" : "Send Back \(n) Windows"
  }

  private func makePanel() -> NSPanel {
    let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 980, height: 420),
                    styleMask: [.titled, .closable, .resizable, .utilityWindow, .nonactivatingPanel],
                    backing: .buffered, defer: false)
    p.title = "Agents' Screen"
    p.level = .floating
    p.isReleasedWhenClosed = false
    p.hidesOnDeactivate = false
    p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
    p.minSize = NSSize(width: 560, height: 260)
    p.delegate = self

    picture.onClick = { point in
      Grabs.grab(at: point)
    }
    sendBack.bezelStyle = .rounded
    sendBack.target = self
    sendBack.action = #selector(sendBackClicked)
    hint.textColor = .secondaryLabelColor
    hint.font = .systemFont(ofSize: 11)
    let bar = NSStackView(views: [hint, NSView(), sendBack])
    bar.orientation = .horizontal
    bar.edgeInsets = NSEdgeInsets(top: 4, left: 8, bottom: 6, right: 8)

    let left = NSStackView(views: [picture, bar])
    left.orientation = .vertical
    left.spacing = 0
    picture.setContentHuggingPriority(.defaultLow, for: .vertical)

    trailView.isEditable = false
    trailView.isSelectable = true
    trailView.drawsBackground = false
    trailView.textContainerInset = NSSize(width: 6, height: 8)
    let scroll = NSScrollView()
    scroll.documentView = trailView
    scroll.hasVerticalScroller = true
    scroll.drawsBackground = false
    trailView.autoresizingMask = [.width]
    trailView.isVerticallyResizable = true
    trailView.textContainer?.widthTracksTextView = true

    let split = NSSplitView()
    split.isVertical = true
    split.dividerStyle = .thin
    split.addArrangedSubview(left)
    split.addArrangedSubview(scroll)
    split.setHoldingPriority(.defaultLow, forSubviewAt: 0)
    split.setHoldingPriority(.defaultHigh, forSubviewAt: 1)
    p.contentView = split
    split.setPosition(680, ofDividerAt: 0)
    if let screen = NSScreen.main?.visibleFrame {
      p.setFrameTopLeftPoint(NSPoint(x: screen.maxX - 1000, y: screen.maxY - 20))
    }
    return p
  }

  @objc private func sendBackClicked() {
    Grabs.sendAllBack()
  }

  private func refreshTrail() {
    guard isOpen else { return }
    let out = NSMutableAttributedString()
    let time = DateFormatter()
    time.dateFormat = "HH:mm:ss"
    let body: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 12), .foregroundColor: NSColor.labelColor]
    let dim: [NSAttributedString.Key: Any] = [.font: NSFont.monospacedDigitSystemFont(ofSize: 10, weight: .regular),
                                              .foregroundColor: NSColor.secondaryLabelColor]
    if Activity.trail.entries.isEmpty {
      out.append(NSAttributedString(string: "Nothing yet. What agents click and type shows here, newest first.", attributes: dim))
    }
    for e in Activity.trail.entries {
      let color = agentColor(e.agent)
      out.append(NSAttributedString(string: "● \(e.agent)", attributes: [.font: NSFont.boldSystemFont(ofSize: 12), .foregroundColor: color]))
      out.append(NSAttributedString(string: "  \(time.string(from: e.time))\(e.count > 1 ? "  ×\(e.count)" : "")\n", attributes: dim))
      out.append(NSAttributedString(string: e.text + "\n\n", attributes: body))
    }
    trailView.textStorage?.setAttributedString(out)
    trailView.scrollToBeginningOfDocument(nil)
  }

  private func refresh() {
    guard !capturing, let id = agentsDisplay(), let panel else { return }
    capturing = true
    let bounds = CGDisplayBounds(id)
    let width = Int(picture.bounds.width * panel.backingScaleFactor)
    picture.display = bounds
    picture.windows = labels(on: bounds)
    Task { @MainActor in
      defer { capturing = false }
      guard let content = try? await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true),
            let display = content.displays.first(where: { $0.displayID == id }) else { return }
      let config = SCStreamConfiguration()
      config.width = max(width, 320)
      config.height = Int(CGFloat(config.width) * bounds.height / bounds.width)
      config.showsCursor = false
      // The harness's own arrows are drawn from what it reports, with the agent's name.
      let arrows = content.windows.filter { $0.title?.hasPrefix("macOS Harness Pointer") == true }
      let filter = SCContentFilter(display: display, excludingWindows: arrows)
      guard let image = try? await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config) else { return }
      picture.image = image
      picture.needsDisplay = true
    }
  }

  /// A label for each window on the display that an agent is using.
  private func labels(on display: CGRect) -> [PeekView.Label] {
    let claims = Claims.all()
    let recent = Date().addingTimeInterval(-600)
    var actors: [pid_t: String] = [:]
    for e in Activity.trail.entries.reversed() where e.time > recent {
      if let app = e.appPid { actors[app] = e.agent }
    }
    return windowsOnAgentDisplays(onScreenOnly: true).reversed().compactMap { w in
      guard display.contains(CGPoint(x: w.frame.midX, y: w.frame.midY)),
            w.frame.width >= 160, w.frame.height >= 100 else { return nil }
      let needs = Labels.needsUser(title: w.title)
      var name: String?
      if w.owner == "Safari" {
        let owner = Labels.safariOwner(title: w.title)
        name = owner.agentPid.map { kill($0, 0) == 0 ? Activity.sessionName($0) : "agent \($0)" } ?? owner.group
      } else if let claim = claims.first(where: { $0.pid == w.pid && ($0.wholeApp || $0.windows.contains(w.id)) }) {
        let owners = claim.owners.filter { kill($0, 0) == 0 }.sorted().map(Activity.sessionName)
        name = owners.isEmpty ? "agent" : owners.joined(separator: ", ")
      }
      name = actors[w.pid] ?? name
      return PeekView.Label(frame: w.frame, text: name ?? w.owner, agent: name, needsUser: needs, title: w.title)
    }
  }
}

func agentColor(_ name: String) -> NSColor {
  let c = Labels.color(of: name)
  return NSColor(srgbRed: c.red, green: c.green, blue: c.blue, alpha: 1)
}

/// The picture of the agents' screen with labels and arrows drawn over it.
final class PeekView: NSView {
  struct Label {
    let frame: CGRect
    let text: String
    /// The agent using the window, nil for a window no agent is known to use.
    let agent: String?
    let needsUser: Bool
    let title: String
  }

  var image: CGImage?
  var display: CGRect = .zero
  var windows: [Label] = []
  var onClick: (CGPoint) -> Void = { _ in }
  private var hover: CGPoint?

  override var isFlipped: Bool { false }

  override func updateTrackingAreas() {
    super.updateTrackingAreas()
    trackingAreas.forEach(removeTrackingArea)
    addTrackingArea(NSTrackingArea(rect: bounds, options: [.mouseMoved, .mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                                   owner: self))
  }

  override func mouseMoved(with event: NSEvent) {
    hover = convert(event.locationInWindow, from: nil)
    needsDisplay = true
  }

  override func mouseExited(with event: NSEvent) {
    hover = nil
    needsDisplay = true
  }

  override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

  override func mouseDown(with event: NSEvent) {
    let p = convert(event.locationInWindow, from: nil)
    guard let point = Geometry.displayPoint(viewPoint: p, shown: shown, display: display) else { return }
    onClick(point)
  }

  private var shown: CGRect {
    Geometry.aspectFit(display.size == .zero ? CGSize(width: 16, height: 9) : display.size, in: bounds.insetBy(dx: 4, dy: 4))
  }

  override func draw(_ dirtyRect: NSRect) {
    NSColor.black.setFill()
    bounds.fill()
    let shown = self.shown
    if let image, let ctx = NSGraphicsContext.current?.cgContext {
      ctx.interpolationQuality = .medium
      ctx.draw(image, in: shown)
    }
    guard display.width > 0 else { return }
    let hovered = hover.flatMap { Geometry.displayPoint(viewPoint: $0, shown: shown, display: display) }
    let top = hovered.flatMap { p in windows.last { $0.frame.contains(p) } }
    for w in windows where w.needsUser {
      NSColor.systemOrange.setStroke()
      let path = NSBezierPath(rect: viewRect(w.frame, shown).insetBy(dx: 1, dy: 1))
      path.lineWidth = 2
      path.stroke()
    }
    // Front windows first; a label that would cover one already placed is left out, so a stack
    // of one agent's windows reads as one label, not a pile.
    var placed: [CGRect] = []
    for w in windows.reversed() {
      let rect = viewRect(w.frame, shown)
      let label = pillText(w.needsUser ? "\(w.text) · needs you" : w.text)
      let size = label.size()
      let pillRect = CGRect(x: rect.minX + 4, y: rect.maxY - 8 - size.height, width: size.width + 10, height: size.height + 4)
      guard !placed.contains(where: { $0.intersects(pillRect) }) else { continue }
      placed.append(pillRect)
      let color = w.agent.map(agentColor) ?? NSColor(white: 0.35, alpha: 1)
      pill(label, in: pillRect, fill: w.needsUser ? .systemOrange : color)
    }
    if let top {
      NSColor.white.withAlphaComponent(0.8).setStroke()
      let path = NSBezierPath(rect: viewRect(top.frame, shown).insetBy(dx: 1, dy: 1))
      path.lineWidth = 2
      path.stroke()
    }
    let now = Date()
    for (name, p) in Activity.pointers.sorted(by: { $0.key < $1.key }) {
      let at = p.position(at: now)
      guard display.contains(at) else { continue }
      let tip = Geometry.viewPoint(displayPoint: at, shown: shown, display: display)
      arrow(at: tip, color: agentColor(name), pressed: p.clickedAt.map { now.timeIntervalSince($0) < 0.15 } ?? false)
      let label = pillText(name)
      let size = label.size()
      pill(label, in: CGRect(x: tip.x + 10, y: tip.y - 22 - size.height, width: size.width + 10, height: size.height + 4),
           fill: NSColor(white: 0.1, alpha: 0.94))
    }
  }

  private func viewRect(_ r: CGRect, _ shown: CGRect) -> CGRect {
    let a = Geometry.viewPoint(displayPoint: CGPoint(x: r.minX, y: r.maxY), shown: shown, display: display)
    let b = Geometry.viewPoint(displayPoint: CGPoint(x: r.maxX, y: r.minY), shown: shown, display: display)
    return CGRect(x: a.x, y: a.y, width: b.x - a.x, height: b.y - a.y)
  }

  /// The harness arrow from pointer.py, tip at `tip`.
  private func arrow(at tip: CGPoint, color: NSColor, pressed: Bool) {
    let points: [CGPoint] = [(5, 5), (5, 16.5), (8, 14.5), (10, 19.25), (11.5, 18.75), (10, 13.75), (13, 13)].map {
      CGPoint(x: $0.0, y: $0.1)
    }
    let scale: CGFloat = pressed ? 1.2 : 1.4
    let path = NSBezierPath()
    for (i, p) in points.enumerated() {
      let q = CGPoint(x: tip.x + (p.x - 5) * scale, y: tip.y - (p.y - 5) * scale)
      i == 0 ? path.move(to: q) : path.line(to: q)
    }
    path.close()
    path.lineJoinStyle = .round
    path.lineWidth = 1.5
    color.setFill()
    path.fill()
    NSColor.white.setStroke()
    path.stroke()
  }

  private func pillText(_ text: String) -> NSAttributedString {
    let attrs: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 10, weight: .medium), .foregroundColor: NSColor.white]
    return NSAttributedString(string: text.count > 40 ? String(text.prefix(39)) + "…" : text, attributes: attrs)
  }

  private func pill(_ text: NSAttributedString, in rect: CGRect, fill: NSColor) {
    fill.setFill()
    NSBezierPath(roundedRect: rect, xRadius: 4, yRadius: 4).fill()
    text.draw(at: CGPoint(x: rect.minX + 5, y: rect.minY + 2))
  }
}

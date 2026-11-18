import CoreGraphics

public enum Geometry {
  /// Where each agent display goes: side by side past the bottom-right corner of the user's
  /// screens, so they touch his screens at a single point and his mouse cannot cross. Nil when
  /// he has no screen of his own to measure from.
  public static func cornerOrigins(real: [CGRect], agentWidths: [CGFloat]) -> [CGPoint]? {
    guard let first = real.first else { return nil }
    let union = real.reduce(first) { $0.union($1) }
    var x = union.maxX
    return agentWidths.map { width in
      defer { x += width }
      return CGPoint(x: x, y: union.maxY)
    }
  }

  /// Whether two screens share a stretch of border the pointer can cross. Screens that meet at
  /// a single corner point do not.
  public static func sharesEdge(_ a: CGRect, _ b: CGRect) -> Bool {
    let xOverlap = min(a.maxX, b.maxX) - max(a.minX, b.minX)
    let yOverlap = min(a.maxY, b.maxY) - max(a.minY, b.minY)
    let sideBySide = (a.maxX == b.minX || b.maxX == a.minX) && yOverlap > 0
    let stacked = (a.maxY == b.minY || b.maxY == a.minY) && xOverlap > 0
    return sideBySide || stacked
  }

  /// Where a window on an agent display lands on the user's screen: at the same offset from the
  /// top-left of `area` (his screen less menu bar and Dock) as it had on the agent display,
  /// shrunk and slid until it fits inside `area`.
  public static func userFrame(window: CGRect, agentBounds: CGRect, area: CGRect) -> CGRect {
    let size = CGSize(width: min(window.width, area.width), height: min(window.height, area.height))
    let origin = CGPoint(
      x: min(max(area.minX + window.minX - agentBounds.minX, area.minX), area.maxX - size.width),
      y: min(max(area.minY + window.minY - agentBounds.minY, area.minY), area.maxY - size.height))
    return CGRect(origin: origin, size: size)
  }

  /// The rect inside `bounds` an image of `content` size takes when scaled to fit, centred.
  public static func aspectFit(_ content: CGSize, in bounds: CGRect) -> CGRect {
    guard content.width > 0, content.height > 0 else { return .zero }
    let scale = min(bounds.width / content.width, bounds.height / content.height)
    let size = CGSize(width: content.width * scale, height: content.height * scale)
    return CGRect(x: bounds.midX - size.width / 2, y: bounds.midY - size.height / 2,
                  width: size.width, height: size.height)
  }

  /// A point on the display (global, top-left origin) for a point in the peek's picture `shown`,
  /// whose y runs bottom-up as AppKit views do; nil outside the picture.
  public static func displayPoint(viewPoint p: CGPoint, shown: CGRect, display: CGRect) -> CGPoint? {
    guard shown.contains(p), shown.width > 0, shown.height > 0 else { return nil }
    return CGPoint(x: display.minX + (p.x - shown.minX) / shown.width * display.width,
                   y: display.minY + (shown.maxY - p.y) / shown.height * display.height)
  }

  /// The inverse of `displayPoint`: a display point in the peek's picture.
  public static func viewPoint(displayPoint p: CGPoint, shown: CGRect, display: CGRect) -> CGPoint {
    CGPoint(x: shown.minX + (p.x - display.minX) / display.width * shown.width,
            y: shown.maxY - (p.y - display.minY) / display.height * shown.height)
  }
}

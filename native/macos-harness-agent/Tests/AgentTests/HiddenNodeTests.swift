import ApplicationServices
import Foundation
import XCTest

@testable import macos_harness_agent

/// A visible-only walk through `AXExecutor.boundedSearch` over a scripted tree, so no live
/// target or trust grant is involved, while the production walk and judgement run unchanged.
final class HiddenNodeTests: XCTestCase {

  private struct Scripted {
    var role: String
    var title: String?
    var hidden: NSNumber?
    var children: [AXUIElement] = []

    func answer(_ name: String) -> AnyObject? {
      switch name {
      case "AXRole": return role as NSString
      case "AXTitle": return title as NSString?
      case "AXHidden": return hidden
      case "AXChildren": return children as NSArray
      default: return nil
      }
    }
  }

  private let root = AXUIElementCreateApplication(90_101)
  private let hiddenByNumber = AXUIElementCreateApplication(90_102)
  private let hiddenByBoolean = AXUIElementCreateApplication(90_103)
  private let shown = AXUIElementCreateApplication(90_104)

  func testAVisibleSearchLeavesOutANodeHiddenByTheNumberOneAsPythonDoes() throws {
    let tree: [AXUIElement: Scripted] = [
      root: Scripted(role: "AXWindow", children: [hiddenByNumber, hiddenByBoolean, shown]),
      hiddenByNumber: Scripted(role: "AXButton", title: "One", hidden: NSNumber(value: 1)),
      hiddenByBoolean: Scripted(role: "AXButton", title: "True", hidden: kCFBooleanTrue),
      shown: Scripted(role: "AXButton", title: "Shown", hidden: NSNumber(value: 0)),
    ]

    let result = try AXExecutor.boundedSearch(
      root: root, searchKey: "AXAnyTypeSearchKey", text: nil, exact: .none, visibleOnly: true,
      limit: 10, direction: "next", immediateDescendantsOnly: false,
      attributes: ["AXTitle"], includeActions: false, maxNodes: 10,
      registry: ElementRegistry()
    ) { element, names in
      var reading = AXExecutor.AttributeValues()
      for name in names {
        reading.record(name, status: .success, value: tree[element]?.answer(name))
      }
      return reading
    }

    XCTAssertEqual(result.matches.map(\.fields), [["title": .string("Shown")]])
  }
}

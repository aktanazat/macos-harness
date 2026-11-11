import ApplicationServices
import Foundation
import XCTest

@testable import macos_harness_agent

/// AppKit answers a password field's `AXValue` with one bullet per character, so even asking
/// for it tells the password's length. These walk a scripted tree through
/// `AXExecutor.boundedSearch` -- each batch answered the way
/// `AXUIElementCopyMultipleAttributeValues` would -- so no live target or trust grant is
/// involved, while the production walk and match description run unchanged.
final class SecureFieldTests: XCTestCase {

  private struct Scripted {
    var role: String
    var subrole: String?
    var value: String?
    var children: [AXUIElement] = []

    func answer(_ name: String) -> AnyObject? {
      switch name {
      case "AXRole": return role as NSString
      case "AXSubrole": return subrole as NSString?
      case "AXValue": return value as NSString?
      case "AXChildren": return children as NSArray
      default: return nil
      }
    }
  }

  private let root = AXUIElementCreateApplication(90_001)
  private let secureSubrole = AXUIElementCreateApplication(90_002)
  private let secureRole = AXUIElementCreateApplication(90_003)
  private let unidentified = AXUIElementCreateApplication(90_004)
  private let plain = AXUIElementCreateApplication(90_005)

  func testASearchForValuesNeverAsksAPasswordFieldForItsValue() throws {
    let bullets = String(repeating: "\u{F79A}", count: 7)
    let tree: [AXUIElement: Scripted] = [
      root: Scripted(
        role: "AXWindow", children: [secureSubrole, secureRole, unidentified, plain]),
      secureSubrole: Scripted(role: "AXTextField", subrole: "AXSecureTextField", value: bullets),
      secureRole: Scripted(role: "AXSecureTextField", value: bullets),
      unidentified: Scripted(role: "AXTextField", value: bullets),
      plain: Scripted(role: "AXTextField", value: "Ada"),
    ]
    var asked: [AXUIElement: [String]] = [:]

    let result = try AXExecutor.boundedSearch(
      root: root, searchKey: "AXAnyTypeSearchKey", text: nil, exact: .none, visibleOnly: true,
      limit: 10, direction: "next", immediateDescendantsOnly: false,
      attributes: ["AXTitle", "AXValue"], includeActions: false, maxNodes: 10,
      registry: ElementRegistry()
    ) { element, names in
      asked[element, default: []] += names
      var reading = AXExecutor.AttributeValues()
      for name in names {
        if element == unidentified && name == "AXSubrole" {
          reading.record(name, status: .failure, value: nil)
        } else {
          reading.record(name, status: .success, value: tree[element]?.answer(name))
        }
      }
      return reading
    }

    XCTAssertEqual(result.matches.map(\.fields), [[:], [:], [:], ["value": .string("Ada")]])
    for field in [secureSubrole, secureRole, unidentified] {
      XCTAssertEqual(
        Set(asked[field, default: []]).intersection(
          ["AXValue", "AXSelectedText", "AXSelectedTextRange", "AXNumberOfCharacters"]),
        [], "\(field) was asked for its contents")
    }
    // A subrole the app could not produce may hide a password field, so that field's value
    // stayed unread and the search cannot claim it saw everything.
    XCTAssertFalse(result.complete)
  }
}

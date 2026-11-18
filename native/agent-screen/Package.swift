// swift-tools-version: 5.9

import PackageDescription

let package = Package(
  name: "agent-screen",
  platforms: [
    .macOS(.v14)
  ],
  targets: [
    // Rules with no AppKit in them: the claims file, where agent displays go, what a grabbed
    // window owes, the trail's wording, and which idle apps a battery pause takes.
    .target(
      name: "AgentScreenCore",
      path: "Sources/AgentScreenCore"
    ),
    // The menu bar companion for the agents' hidden screen.
    .executableTarget(
      name: "agent-screen",
      dependencies: ["AgentScreenCore"],
      path: "Sources/agent-screen",
      linkerSettings: [
        .linkedFramework("AppKit"),
        .linkedFramework("ApplicationServices"),
        .linkedFramework("Carbon"),
        .linkedFramework("IOKit"),
        .linkedFramework("ScreenCaptureKit"),
      ]
    ),
    .testTarget(
      name: "AgentScreenTests",
      dependencies: ["AgentScreenCore"],
      path: "Tests/AgentScreenTests"
    ),
  ]
)

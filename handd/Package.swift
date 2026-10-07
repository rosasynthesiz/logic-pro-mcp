// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "handd",
    platforms: [
        .macOS(.v14)
    ],
    targets: [
        .executableTarget(
            name: "handd",
            path: "Sources/handd"
        )
    ]
)

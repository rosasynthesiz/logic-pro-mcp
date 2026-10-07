// handd — Phase 1 sensor for gesture control of Logic Pro.
//
// Captures the Mac's camera, runs Apple's Vision hand-pose detector on each frame, and streams
// normalised 21-point hand landmarks as JSON-lines-over-UDP datagrams to a local listener
// (default 127.0.0.1:9101). See README.md for the exact wire format.
//
// Dependencies: AVFoundation (capture), Vision (hand pose), Foundation, and the Darwin POSIX
// socket API for UDP. No third-party packages.

import AVFoundation
import CoreMedia
import CoreVideo
import Darwin
import Foundation
import Vision

// MARK: - stderr helpers

func eprint(_ s: String) {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
}

func printUsage() {
    eprint("""
    handd — Vision hand-pose landmarks over UDP, for gesture control of Logic Pro.

    Usage: handd [options]

      --port <n>        UDP destination port (default 9101)
      --host <addr>     UDP destination host (default 127.0.0.1)
      --max-hands <n>   Maximum simultaneous hands to track (default 2)
      --fps <n>         Target emit rate, throttled in software (default 60)
      --mirror          Flip x so a user-facing camera reads "hand moves right" as x increasing (default on)
      --no-mirror       Disable mirroring
      --device <text>   Substring match (case-insensitive) on camera name to pick a specific device
      --list-devices    List available camera devices and exit
      --quiet           Suppress the startup banner on stderr
      --help            Show this message
    """)
}

// MARK: - CLI options

struct Options {
    var port: UInt16 = 9101
    var host: String = "127.0.0.1"
    var maxHands: Int = 2
    var fps: Double = 60
    var mirror: Bool = true
    var deviceSubstring: String? = nil
    var listDevices: Bool = false
    var quiet: Bool = false
}

func parseArgs(_ args: [String]) -> Options {
    var opts = Options()
    var i = 0
    while i < args.count {
        let a = args[i]
        switch a {
        case "--port":
            i += 1
            guard i < args.count, let v = UInt16(args[i]) else {
                eprint("handd: --port needs an integer 0-65535"); exit(64)
            }
            opts.port = v
        case "--host":
            i += 1
            guard i < args.count else { eprint("handd: --host needs a value"); exit(64) }
            opts.host = args[i]
        case "--max-hands":
            i += 1
            guard i < args.count, let v = Int(args[i]), v > 0 else {
                eprint("handd: --max-hands needs a positive integer"); exit(64)
            }
            opts.maxHands = v
        case "--fps":
            i += 1
            guard i < args.count, let v = Double(args[i]), v > 0 else {
                eprint("handd: --fps needs a positive number"); exit(64)
            }
            opts.fps = v
        case "--mirror":
            opts.mirror = true
        case "--no-mirror":
            opts.mirror = false
        case "--device":
            i += 1
            guard i < args.count else { eprint("handd: --device needs a value"); exit(64) }
            opts.deviceSubstring = args[i]
        case "--list-devices":
            opts.listDevices = true
        case "--quiet":
            opts.quiet = true
        case "--help", "-h":
            printUsage()
            exit(0)
        default:
            eprint("handd: unknown argument '\(a)'")
            printUsage()
            exit(64)
        }
        i += 1
    }
    return opts
}

// MARK: - Number formatting

// Fixed-point, non-exponential formatting so every number in the JSON line parses trivially
// on the consumer side regardless of magnitude.
func fmt(_ value: Double) -> String {
    if value.isNaN || value.isInfinite { return "0.0" }
    return String(format: "%.6f", value)
}

// MARK: - UDP sender (raw POSIX socket; no Network.framework dependency)

final class UDPSender {
    private let fd: Int32
    private var destAddr: sockaddr_in

    init?(host: String, port: UInt16) {
        let socketFD = socket(AF_INET, SOCK_DGRAM, 0)
        guard socketFD >= 0 else { return nil }

        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        let pton = host.withCString { cstr in
            inet_pton(AF_INET, cstr, &addr.sin_addr)
        }
        guard pton == 1 else {
            close(socketFD)
            return nil
        }

        self.fd = socketFD
        self.destAddr = addr
    }

    func send(_ data: Data) {
        var addr = destAddr
        _ = data.withUnsafeBytes { rawBuf -> Int in
            withUnsafePointer(to: &addr) { addrPtr -> Int in
                addrPtr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockaddrPtr in
                    sendto(fd, rawBuf.baseAddress, data.count, 0, sockaddrPtr, socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
        }
        // Best-effort UDP: a dropped datagram just means the consumer misses one tick at 60fps.
        // Not worth surfacing per-packet errors on a fire-and-forget local socket.
    }

    func closeSocket() {
        close(fd)
    }
}

// MARK: - Camera discovery / permission

func discoverVideoDevices() -> [AVCaptureDevice] {
    let types: [AVCaptureDevice.DeviceType] = [.builtInWideAngleCamera, .external, .continuityCamera]
    let discovery = AVCaptureDevice.DiscoverySession(deviceTypes: types, mediaType: .video, position: .unspecified)
    return discovery.devices
}

func selectDevice(substring: String?) -> AVCaptureDevice? {
    let devices = discoverVideoDevices()
    if let substring = substring {
        return devices.first { $0.localizedName.localizedCaseInsensitiveContains(substring) }
    }
    return AVCaptureDevice.default(for: .video) ?? devices.first
}

func ensureCameraAccess() -> Bool {
    switch AVCaptureDevice.authorizationStatus(for: .video) {
    case .authorized:
        return true
    case .notDetermined:
        let sem = DispatchSemaphore(value: 0)
        var granted = false
        AVCaptureDevice.requestAccess(for: .video) { ok in
            granted = ok
            sem.signal()
        }
        sem.wait()
        return granted
    case .denied, .restricted:
        return false
    @unknown default:
        return false
    }
}

// MARK: - Vision hand pose -> wire format

// Fixed joint order for the "lm" array. This is Vision's own skeletal order, which happens to
// match MediaPipe's canonical 21-point hand order: wrist, then thumb/index/middle/ring/little,
// each proximal-to-distal.
//
// NOTE (judgement call): Vision names the thumb's second joint `.thumbMP` (metacarpophalangeal),
// not `.thumbMCP` — the wire contract calls it "thumb MCP" for consistency with the other fingers,
// but it's the same joint; `.thumbMP` is what we read from the API.
let jointOrder: [VNHumanHandPoseObservation.JointName] = [
    .wrist,
    .thumbCMC, .thumbMP, .thumbIP, .thumbTip,
    .indexMCP, .indexPIP, .indexDIP, .indexTip,
    .middleMCP, .middlePIP, .middleDIP, .middleTip,
    .ringMCP, .ringPIP, .ringDIP, .ringTip,
    .littleMCP, .littlePIP, .littleDIP, .littleTip,
]

func chiralityString(_ c: VNChirality) -> String {
    switch c {
    case .left: return "left"
    case .right: return "right"
    default: return "unknown"
    }
}

// Builds one hand's JSON object. Coordinates are normalised 0..1; Vision reports y from the
// bottom of the image, so we flip to `1 - y` to give downstream consumers a top-left origin.
// z has no true depth from Vision, so it is always 0.0 (kept as a triple for a future 3D sensor).
//
// Mirroring (x = 1 - x) is a display/UX convention only: it does not affect `chirality`. Vision
// determines handedness from the hand's visual structure in the raw (unmirrored) camera buffer,
// and a front-facing camera is a projection of the scene, not an optical mirror — a raised right
// hand is classified "right" regardless of which side of the frame it lands on.
func jsonForObservation(_ obs: VNHumanHandPoseObservation, mirror: Bool) -> String? {
    guard let points = try? obs.recognizedPoints(.all) else { return nil }

    var lmParts: [String] = []
    lmParts.reserveCapacity(jointOrder.count)
    for joint in jointOrder {
        if let p = points[joint] {
            var x = Double(p.location.x)
            let y = 1.0 - Double(p.location.y)
            if mirror { x = 1.0 - x }
            lmParts.append("[\(fmt(x)),\(fmt(y)),0.0]")
        } else {
            // Shouldn't normally happen (recognizedPoints(.all) returns the full skeleton), but
            // keep the triple-shape contract intact if a joint is ever missing.
            lmParts.append("[0.0,0.0,0.0]")
        }
    }

    let chirality = chiralityString(obs.chirality)
    let score = Double(obs.confidence)
    return "{\"chirality\":\"\(chirality)\",\"score\":\(fmt(score)),\"lm\":[\(lmParts.joined(separator: ","))]}"
}

// MARK: - Capture delegate

final class HandPoseCapture: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    private let options: Options
    private let sender: UDPSender
    private let request: VNDetectHumanHandPoseRequest
    private let minInterval: TimeInterval
    private var lastEmit: TimeInterval = 0

    init(options: Options, sender: UDPSender) {
        self.options = options
        self.sender = sender

        let req = VNDetectHumanHandPoseRequest()
        // maximumHandCount directly costs latency: Vision runs one full hand-pose inference
        // pass per detected hand, so tracking 2 hands roughly doubles per-frame compute versus 1.
        req.maximumHandCount = options.maxHands
        self.request = req

        self.minInterval = options.fps > 0 ? 1.0 / options.fps : 0
        super.init()
    }

    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        let now = ProcessInfo.processInfo.systemUptime
        if now - lastEmit < minInterval {
            return // software throttle to --fps; alwaysDiscardsLateVideoFrames already sheds backlog upstream
        }
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }

        var handsJSON: [String] = []
        let handler = VNImageRequestHandler(cvPixelBuffer: pixelBuffer, orientation: .up, options: [:])
        do {
            try handler.perform([request])
            if let observations = request.results {
                for obs in observations {
                    if let json = jsonForObservation(obs, mirror: options.mirror) {
                        handsJSON.append(json)
                    }
                }
            }
        } catch {
            // Vision failed on this one frame — fall through and emit "hands":[] below rather
            // than dropping the datagram. The consumer needs a heartbeat to know the hand is
            // GONE, not just silence.
        }

        lastEmit = now
        let line = "{\"t\":\(fmt(now)),\"source\":\"handd\",\"hands\":[\(handsJSON.joined(separator: ","))]}"
        if let data = line.data(using: .utf8) {
            sender.send(data)
        }
    }
}

// MARK: - main

let opts = parseArgs(Array(CommandLine.arguments.dropFirst()))

if opts.listDevices {
    let devices = discoverVideoDevices()
    if devices.isEmpty {
        print("(no camera devices found)")
    }
    for d in devices {
        print("\(d.localizedName)\t\(d.uniqueID)")
    }
    exit(0)
}

guard ensureCameraAccess() else {
    eprint("handd: camera access was denied.")
    eprint("Grant camera access in System Settings > Privacy & Security > Camera for the")
    eprint("terminal app you launched handd from (e.g. Terminal, iTerm), then re-run handd.")
    exit(1)
}

guard let device = selectDevice(substring: opts.deviceSubstring) else {
    if let sub = opts.deviceSubstring {
        eprint("handd: no camera found matching '\(sub)'. Try --list-devices.")
    } else {
        eprint("handd: no camera found.")
    }
    exit(1)
}

let session = AVCaptureSession()
session.beginConfiguration()

// Prefer 1280x720 for speed over resolution; fall back gracefully if unsupported.
if session.canSetSessionPreset(.hd1280x720) {
    session.sessionPreset = .hd1280x720
} else {
    session.sessionPreset = .high
}

do {
    let input = try AVCaptureDeviceInput(device: device)
    guard session.canAddInput(input) else {
        eprint("handd: could not add camera input for '\(device.localizedName)'")
        exit(1)
    }
    session.addInput(input)
} catch {
    eprint("handd: failed to open camera '\(device.localizedName)': \(error.localizedDescription)")
    exit(1)
}

// Push the device's frame duration toward --fps, but ONLY to a rate the active format actually
// advertises.
//
// This has to be a check, not a try/catch. `setActiveVideoMinFrameDuration:` raises an Objective-C
// NSException for an unsupported rate, and Swift cannot catch those — the `do/catch` here only ever
// covered `lockForConfiguration()`. Asking a built-in MacBook camera (15-30 fps) for the default 60
// therefore killed the process outright:
//
//   *** Terminating app due to uncaught exception 'NSInvalidArgumentException' ... tried to set
//   maxFrameRate to 60.000000
//
// So: clamp into a supported range first, and say so rather than silently running slower than asked.
do {
    try device.lockForConfiguration()
    defer { device.unlockForConfiguration() }
    let ranges = device.activeFormat.videoSupportedFrameRateRanges
    let covering = ranges.first { opts.fps >= $0.minFrameRate && opts.fps <= $0.maxFrameRate }
    let fastest = ranges.max { $0.maxFrameRate < $1.maxFrameRate }
    if let range = covering ?? fastest {
        let target = min(max(opts.fps, range.minFrameRate), range.maxFrameRate)
        let wanted = CMTime(value: 1, timescale: CMTimeScale(max(1, Int(target.rounded()))))
        device.activeVideoMinFrameDuration = wanted
        device.activeVideoMaxFrameDuration = wanted
        if target < opts.fps && !opts.quiet {
            eprint("handd: camera tops out at \(Int(target)) fps; --fps \(Int(opts.fps)) clamped to that")
        }
    }
} catch {
    // Could not lock the device; keep whatever the hardware defaults to.
}

guard let sender = UDPSender(host: opts.host, port: opts.port) else {
    eprint("handd: could not open a UDP socket to \(opts.host):\(opts.port)")
    exit(1)
}

let capture = HandPoseCapture(options: opts, sender: sender)
let videoQueue = DispatchQueue(label: "handd.video.queue")
let output = AVCaptureVideoDataOutput()
output.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: Int(kCVPixelFormatType_32BGRA)]
output.alwaysDiscardsLateVideoFrames = true // a late hand frame is worth less than the one behind it
output.setSampleBufferDelegate(capture, queue: videoQueue)

guard session.canAddOutput(output) else {
    eprint("handd: could not add video output")
    exit(1)
}
session.addOutput(output)
session.commitConfiguration()

if !opts.quiet {
    eprint(
        "handd: device=\"\(device.localizedName)\" resolution=1280x720 max-hands=\(opts.maxHands) "
        + "target-fps=\(Int(opts.fps)) mirror=\(opts.mirror) -> udp \(opts.host):\(opts.port)"
    )
}

// Clean SIGINT handling: stop the session and exit rather than being torn down mid-frame.
signal(SIGINT, SIG_IGN)
let sigintSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
sigintSource.setEventHandler {
    if !opts.quiet {
        eprint("handd: stopping (SIGINT)")
    }
    session.stopRunning()
    sender.closeSocket()
    exit(0)
}
sigintSource.resume()

session.startRunning()
RunLoop.main.run()

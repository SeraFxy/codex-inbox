import AppKit
// 按 macOS 图标模板裁成圆角方形：1024 画布，主体 824，四周各留 100
let args = CommandLine.arguments
guard let src = NSImage(contentsOfFile: args[1]) else { exit(1) }
let size = 1024.0, body = 824.0, inset = (size - body) / 2
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 1024, pixelsHigh: 1024, bitsPerSample: 8, samplesPerPixel: 4,
                           hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
let rect = NSRect(x: inset, y: inset, width: body, height: body)
NSBezierPath(roundedRect: rect, xRadius: 185, yRadius: 185).addClip()
src.draw(in: rect, from: .zero, operation: .copy, fraction: 1)
NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: args[2]))

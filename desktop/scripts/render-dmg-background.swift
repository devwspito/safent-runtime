// Rebuild with: swift desktop/scripts/render-dmg-background.swift OUTPUT.png
// Native vector/text rendering, no network assets or new runtime dependency.
import AppKit

guard CommandLine.arguments.count == 2 else {
    fatalError("Usage: render-dmg-background.swift OUTPUT.png")
}
let width = 660
let height = 400
let bitmap = NSBitmapImageRep(
    bitmapDataPlanes: nil, pixelsWide: width, pixelsHigh: height,
    bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
    isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
)!
let graphics = NSGraphicsContext(bitmapImageRep: bitmap)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = graphics
let context = graphics.cgContext
context.setFillColor(CGColor(gray: 0.98, alpha: 1))
context.fill(CGRect(x: 0, y: 0, width: width, height: height))

func text(_ value: String, top: CGFloat, size: CGFloat, weight: NSFont.Weight, color: NSColor) {
    let paragraph = NSMutableParagraphStyle()
    paragraph.alignment = .center
    let attributes: [NSAttributedString.Key: Any] = [
        .font: NSFont.systemFont(ofSize: size, weight: weight),
        .foregroundColor: color,
        .paragraphStyle: paragraph,
    ]
    (value as NSString).draw(
        in: NSRect(x: 30, y: CGFloat(height) - top - size * 1.5,
                   width: CGFloat(width) - 60, height: size * 1.5),
        withAttributes: attributes
    )
}

text("Instala Safent", top: 38, size: 30, weight: .semibold,
     color: NSColor(calibratedWhite: 0.13, alpha: 1))
text("Arrastra Safent a la carpeta Aplicaciones.", top: 86, size: 15, weight: .regular,
     color: NSColor(calibratedWhite: 0.35, alpha: 1))

// The actual app and Applications shortcut are Finder items, never painted icons.
context.setStrokeColor(CGColor(gray: 0.50, alpha: 1))
context.setLineWidth(2.5)
context.setLineCap(.round)
context.setLineJoin(.round)
context.move(to: CGPoint(x: 307, y: 200))
context.addLine(to: CGPoint(x: 353, y: 200))
context.move(to: CGPoint(x: 343, y: 210))
context.addLine(to: CGPoint(x: 353, y: 200))
context.addLine(to: CGPoint(x: 343, y: 190))
context.strokePath()

text("Después, ábrelo desde Aplicaciones o Launchpad.", top: 331, size: 13, weight: .regular,
     color: NSColor(calibratedWhite: 0.35, alpha: 1))
NSGraphicsContext.restoreGraphicsState()
let png = bitmap.representation(using: .png, properties: [:])!
try png.write(to: URL(fileURLWithPath: CommandLine.arguments[1]), options: .atomic)

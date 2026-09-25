import AppKit
import Foundation

struct Caption: Decodable {
    let text: String
    let file: String
}

let input = URL(fileURLWithPath: CommandLine.arguments[1])
let output = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
let captions = try JSONDecoder().decode([Caption].self, from: Data(contentsOf: input))
let size = NSSize(width: 1080, height: 260)

for caption in captions {
    let image = NSImage(size: size)
    image.lockFocus()
    NSColor.clear.setFill()
    NSRect(origin: .zero, size: size).fill()
    if !caption.text.isEmpty {
        let paragraph = NSMutableParagraphStyle()
        paragraph.alignment = .center
        paragraph.lineBreakMode = .byWordWrapping
        let shadow = NSShadow()
        shadow.shadowColor = NSColor.black.withAlphaComponent(0.95)
        shadow.shadowBlurRadius = 11
        shadow.shadowOffset = .zero
        let attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 69, weight: .heavy),
            .foregroundColor: NSColor.white,
            .paragraphStyle: paragraph,
            .shadow: shadow,
        ]
        let text = NSAttributedString(string: caption.text, attributes: attributes)
        text.draw(in: NSRect(x: 65, y: 38, width: 950, height: 185))
    }
    image.unlockFocus()
    guard let data = image.tiffRepresentation,
          let bitmap = NSBitmapImageRep(data: data),
          let png = bitmap.representation(using: .png, properties: [:]) else {
        fatalError("No se pudo crear PNG")
    }
    try png.write(to: output.appendingPathComponent(caption.file))
}

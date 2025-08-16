```dataviewjs
const FOLDER = "Clippings"
const CSS = "font-size:medium;"

const p = dv.el("input","")
p.placeholder = "..."
p.style = "width:50%;font-size:large;border-radius:3px;"
const b = dv.el("div", "")
b.style = "max-height:14000px;"
disp()

p.onkeyup = () => disp()

function disp(){
  const  d = dv.pages(`"${FOLDER}"`)
  .filter(x => x.title)
  .filter(x => (x.title + x.author + x.description).includes(p.value))
  .sort(x => x.file.mtime, "desc")
  .limit(200)
  .map(x => `<tr style="${CSS}"><td style="width:20%;"><a class=external-link href='${(x.source)}'><img style="max-height: 100px;" alt="🌏️" src="${x.image || ''}"></a></td><td><a class=internal-link href="${x.file.name}">${x.title}</a><br>${x.description || "..."}</td></tr>`)
  b.innerHTML = `<br><table style='width:100%;'>${d.join("\n")}</table>`
}
```

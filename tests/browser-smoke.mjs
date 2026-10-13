import { chromium } from "playwright";
import assert from "node:assert/strict";
const browser = await chromium.launch({ headless: true });
const checks = [];
const fixture = {
 family:{name:"Тест"}, totals:{TRY:1000,RUB:1000},
 accounts:[{id:"a",name:"Банк TRY",currency:"TRY",kind:"bank",balance:0,is_active:true}],
 allAccounts:[{id:"a",name:"Банк TRY",currency:"TRY",kind:"bank",opening_balance:0,is_active:true}],
 operations:[], month:{expense:{TRY:0,RUB:0},income:{TRY:0,RUB:0},categories:[]},
 structure:[{label:"Операции",value:0}],
 categoryStructure:[{id:"c",name:"Продукты",kind:"expense",is_active:true,aliasCount:0,aliasDetails:[]}],
 allCategories:[{id:"c",name:"Продукты",kind:"expense",is_active:true}],topAliases:[],recurringPayments:[]
};
async function openPage(role, viewport) {
 const context=await browser.newContext({viewport});
 const page=await context.newPage(); const writes=[];
 await page.route("**/functions/v1/family-finance-dashboard-api**",async route=>{
  const req=route.request(),u=new URL(req.url()),path=u.pathname.split("/family-finance-dashboard-api").pop()||"/";
  if(req.method()==="GET"&&path==="/dashboard") return route.fulfill({status:200,json:fixture});
  if(req.method()==="GET"&&path==="/dictionary") return route.fulfill({status:200,json:{allAccounts:fixture.allAccounts,categoryStructure:fixture.categoryStructure,allCategories:fixture.allCategories,topAliases:[],structure:fixture.structure}});
  if(req.method()==="GET"&&path==="/recurring/list") return route.fulfill({status:200,json:{recurringPayments:[]}});
  if(req.method()==="POST"){writes.push(path);return route.fulfill({status:500,json:{error:"Write blocked by test"}});}
  return route.fulfill({status:404,json:{error:path}});
 });
 const token=role==="owner"?"a.owner.b":"a.member:test.b";
 await page.goto((process.env.SMOKE_BASE_URL||"http://127.0.0.1:4173/")+"#token="+encodeURIComponent(token));
 await page.getByText("Данные загружены").waitFor({timeout:10000});
 return {context,page,writes};
}
try {
 const m=await openPage("member",{width:1280,height:900});
 assert.equal(await m.page.locator("#adminNav").evaluate(e=>e.classList.contains("hidden")),true);
 assert.match(await m.page.locator("#accounts").innerText(),/Банк TRY/);
 await m.page.getByRole("button",{name:/Операция/}).first().click();
 await m.page.locator("#oAmount").fill("10");
 await m.page.locator("#oAmount").dispatchEvent("input");
 assert.match(await m.page.locator("#balanceWarning").innerText(),/Недостаточно средств/);
 await m.page.getByRole("button",{name:"Сохранить"}).click();
 assert.match(await m.page.locator("#formError").innerText(),/Недостаточно средств/);
 assert.equal(m.writes.length,0);
 checks.push("PASS member dashboard, role visibility and client balance guard");
 await m.context.close();

 const o=await openPage("owner",{width:1280,height:900});
 assert.equal(await o.page.locator("#adminNav").evaluate(e=>!e.classList.contains("hidden")),true);
 checks.push("PASS owner administration visibility");
 await o.context.close();

 const mobile=await openPage("member",{width:390,height:844});
 const dims=await mobile.page.evaluate(()=>({w:document.documentElement.clientWidth,s:document.documentElement.scrollWidth}));
 assert.ok(dims.s<=dims.w+1,JSON.stringify(dims));
 await mobile.page.getByRole("button",{name:/Операция/}).first().click();
 const modal=await mobile.page.locator("#modal .modal-card").evaluate(e=>Math.round(e.getBoundingClientRect().width));
 assert.ok(modal<=390,"modal width "+modal);
 checks.push("PASS mobile page and operation modal fit viewport");
 await mobile.context.close();
 for(const x of checks) console.log(x);
 console.log("Browser checks passed; all API writes intercepted.");
} finally { await browser.close(); }

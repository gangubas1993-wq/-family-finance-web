import { chromium } from "playwright";
import assert from "node:assert/strict";

const browser = await chromium.launch({ headless: true });
const results = [];
const fixture = {
  family: { name: "Изолированный тест" },
  totals: { TRY: 0, RUB: 0 },
  accounts: [
    { id: "a", name: "Тестовый банк TRY", currency: "TRY", kind: "bank", balance: 0, is_active: true }
  ],
  allAccounts: [
    { id: "a", name: "Тестовый банк TRY", currency: "TRY", kind: "bank", opening_balance: 0, is_active: true }
  ],
  operations: [],
  month: { expense: { TRY: 0, RUB: 0 }, income: { TRY: 0, RUB: 0 }, categories: [] },
  structure: [{ label: "Операции", value: 0 }],
  categoryStructure: [
    { id: "c", name: "Тестовые продукты", kind: "expense", is_active: true, aliasCount: 0, aliasDetails: [] }
  ],
  allCategories: [
    { id: "c", name: "Тестовые продукты", kind: "expense", is_active: true }
  ],
  topAliases: [],
  recurringPayments: []
};

async function openIsolatedPage(role, viewport) {
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  const interceptedWrites = [];

  await page.route("**/functions/v1/family-finance-dashboard-api**", async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.split("/family-finance-dashboard-api").pop() || "/";

    if (request.method() === "OPTIONS") {
      return route.fulfill({
        status: 204,
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Headers": "authorization,content-type",
          "Access-Control-Allow-Methods": "GET,POST,OPTIONS"
        }
      });
    }
    if (request.method() === "GET" && path === "/dashboard") {
      return route.fulfill({ status: 200, json: fixture });
    }
    if (request.method() === "GET" && path === "/dictionary") {
      return route.fulfill({
        status: 200,
        json: {
          allAccounts: fixture.allAccounts,
          categoryStructure: fixture.categoryStructure,
          allCategories: fixture.allCategories,
          topAliases: [],
          structure: fixture.structure
        }
      });
    }
    if (request.method() === "GET" && path === "/recurring/list") {
      return route.fulfill({ status: 200, json: { recurringPayments: [] } });
    }
    if (request.method() === "POST") {
      interceptedWrites.push(path);
      return route.fulfill({ status: 403, json: { error: "Запись запрещена изолированным тестом" } });
    }
    return route.fulfill({ status: 404, json: { error: "Неизвестный тестовый маршрут: " + path } });
  });

  const token = role === "owner" ? "test.owner.signature" : "test.member:test.signature";
  await page.goto((process.env.SMOKE_BASE_URL || "http://127.0.0.1:4173/") + "#token=" + encodeURIComponent(token));
  await page.waitForFunction(() => document.querySelector("#totals")?.innerText.includes("Всего TRY"), null, { timeout: 10000 });
  return { context, page, interceptedWrites };
}

try {
  const member = await openIsolatedPage("member", { width: 1280, height: 900 });
  assert.equal(
    await member.page.locator("#adminNav").evaluate(element => element.classList.contains("hidden")),
    true,
    "участник не должен видеть управление"
  );
  assert.match(await member.page.locator("#totals").innerText(), /Всего TRY/);
  await member.page.locator('button[onclick="openCreate()"]').first().click();
  await member.page.locator("#oAmount").fill("10");
  assert.match(await member.page.locator("#balanceWarning").innerText(), /Недостаточно средств/);
  await member.page.locator("#modal button[onclick=" + '"saveOperation()"' + "]").click();
  assert.match(await member.page.locator("#formError").innerText(), /Недостаточно средств/);
  assert.equal(member.interceptedWrites.length, 0, "операция с недостаточным балансом не должна отправляться");
  results.push("PASS: участник, баланс 0, блокировка расхода до API");
  await member.context.close();

  const owner = await openIsolatedPage("owner", { width: 1280, height: 900 });
  assert.equal(
    await owner.page.locator("#adminNav").evaluate(element => !element.classList.contains("hidden")),
    true,
    "владелец должен видеть управление"
  );
  await owner.page.locator("#adminNav").click();
  assert.match(await owner.page.locator("#accounts").innerText(), /Тестовый банк TRY/);
  results.push("PASS: владелец видит управление и тестовый счёт");
  await owner.context.close();

  const mobile = await openIsolatedPage("member", { width: 390, height: 844 });
  const dimensions = await mobile.page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    document: document.documentElement.scrollWidth
  }));
  assert.ok(
    dimensions.document <= dimensions.viewport + 1,
    "горизонтальное переполнение на мобильном экране: " + JSON.stringify(dimensions)
  );
  await mobile.page.locator('button[onclick="openCreate()"]').first().click();
  const modalWidth = await mobile.page.locator("#modal .modal-card").evaluate(element => Math.round(element.getBoundingClientRect().width));
  assert.ok(modalWidth <= 390, "модальное окно шире мобильного экрана: " + modalWidth);
  results.push("PASS: мобильная ширина и окно операции");
  await mobile.context.close();

  for (const result of results) console.log(result);
  console.log("Все браузерные проверки завершены; все запросы на запись изолированы и перехвачены.");
} finally {
  await browser.close();
}

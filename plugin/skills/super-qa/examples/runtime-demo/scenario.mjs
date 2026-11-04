// Same user action/data/assertions against both real local app versions.
export async function run(page) {
  await page.getByRole('button', {name: 'Compute total'}).click();
  await page.getByRole('status').filter({hasText: 'Total: 5050'}).waitFor();
  return {correct: await page.getByRole('status').textContent() === 'Total: 5050'};
}

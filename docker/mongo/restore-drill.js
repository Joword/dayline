// Restore drill, part 2 (run by `make restore-drill` after mongorestore into dayline_drill):
// compare document counts per collection with the live database, then drop the copy.
// Counts can legitimately be higher in the live DB (data written since the backup);
// a collection missing from the drill copy, or a drill count above live, is a problem.
const live = db.getSiblingDB("dayline");
const drill = db.getSiblingDB("dayline_drill");
let problems = 0;
const names = new Set([...live.getCollectionNames(), ...drill.getCollectionNames()]);
print("collection".padEnd(18) + "backup".padStart(10) + "live".padStart(10));
for (const c of [...names].sort()) {
  const b = drill.getCollectionNames().includes(c) ? drill[c].countDocuments() : -1;
  const l = live.getCollectionNames().includes(c) ? live[c].countDocuments() : -1;
  const flag = b < 0 || b > l ? "  <-- check" : "";
  if (flag) problems++;
  print(c.padEnd(18) + String(b).padStart(10) + String(l).padStart(10) + flag);
}
const idx = drill.reports ? drill.reports.getIndexes().map((i) => i.name) : [];
print("reports indexes in backup: " + idx.join(", "));
drill.dropDatabase();
print(problems ? `restore drill: ${problems} collection(s) to check` : "restore drill: OK");
quit(problems ? 1 : 0);

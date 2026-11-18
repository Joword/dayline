// Run once by the `mongo-init` service: initiate the single-node replica set
// (needed for transactions and change streams) and wait until it is writable.
const cfg = { _id: "rs0", members: [{ _id: 0, host: "mongo:27017" }] };

try {
  rs.status();
  print("replica set already initialised");
} catch (e) {
  if (e.codeName !== "NotYetInitialized" && !String(e).includes("no replset config")) throw e;
  rs.initiate(cfg);
  print("replica set initiated");
}

for (let i = 0; i < 60; i++) {
  if (db.hello().isWritablePrimary) {
    print("primary ready");
    quit(0);
  }
  sleep(500);
}
print("timed out waiting for primary");
quit(1);

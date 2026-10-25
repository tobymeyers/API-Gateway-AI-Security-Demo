// Check for a newer release before the event. Keep in lockstep with
// build.sbt's gatling core version - 4.13.1 is the gatling-sbt release
// that shipped alongside gatling core 3.13.5 (both released within two
// days of each other, 2025-03-11/13).
addSbtPlugin("io.gatling" % "gatling-sbt" % "4.13.1")
